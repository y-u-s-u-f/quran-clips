// pipeline/gpu/bars_gpu.swift -- the `bars` filtergraph on the GPU.
//
// render_bars.py builds the bars look as an ffmpeg filtergraph; this binary
// runs THAT graph -- the same -filter_complex string and input argv, read from
// a JSON plan -- on Metal, so every constant stays in Python and the graph
// stays the spec. Each filter is a port of its ffmpeg 9.0.2 C (or, where the
// arm64 build dispatches to it, NEON) implementation in the same order and
// precision: swscale's initFilter / hscale / vscale / yuv2rgb, eq's LUT,
// colorbalance's float path, vignette's dither LCG, gblur's IIR, ... --
// pinned against ffmpeg by tests/gpu_parity.py. Only the filters and
// argument forms the bars graph uses are implemented; anything else is a
// loud error, never a guess.
//
// usage: bars_gpu PLAN.json OUT [--raw] [--tap LABEL] [--frames N]
//   OUT    video-only H.264 (VideoToolbox) .mov, or with --raw the
//          uncompressed planes of every frame (ffmpeg plane order)
//   --tap  render LABEL instead of [vout]; prints its pixel format
// Built by ./install.sh into tools/bars_gpu.
import AVFoundation
import CoreVideo
import Foundation
import ImageIO
import Metal

func die(_ s: String) -> Never {
    FileHandle.standardError.write(("bars_gpu: " + s + "\n").data(using: .utf8)!)
    exit(1)
}

// ---------------------------------------------------------------------------
// av_expr subset (libavutil/eval.c): numbers, variables, + - * / with
// ffmpeg's sign-as-factor parsing, and the functions the graph uses.
// ---------------------------------------------------------------------------
final class Expr {
    enum Op { case num(Double), v(String), add, mul, div, fn(String) }
    let op: Op
    var sign = 1.0
    var a: [Expr] = []
    init(_ op: Op, _ a: [Expr] = []) { self.op = op; self.a = a }

    func eval(_ vars: [String: Double]) -> Double {
        let r: Double
        switch op {
        case .num(let d): r = d
        case .v(let n):
            guard let d = vars[n] else { die("expression variable \(n) unknown") }
            r = d
        case .add: r = a[0].eval(vars) + a[1].eval(vars)
        case .mul: r = a[0].eval(vars) * a[1].eval(vars)
        case .div:
            let d2 = a[1].eval(vars), d = a[0].eval(vars)
            r = d2 != 0 ? d / d2 : d * .infinity
        case .fn(let n):
            switch n {
            case "if": r = a[0].eval(vars) != 0 ? a[1].eval(vars) : (a.count > 2 ? a[2].eval(vars) : 0)
            case "lt": r = a[0].eval(vars) < a[1].eval(vars) ? 1 : 0
            case "max": let d = a[0].eval(vars), d2 = a[1].eval(vars); r = d > d2 ? d : d2
            case "min": let d = a[0].eval(vars), d2 = a[1].eval(vars); r = d < d2 ? d : d2
            case "between":
                let d = a[0].eval(vars)
                r = (d >= a[1].eval(vars) && d <= a[2].eval(vars)) ? 1 : 0
            case "clip":
                let x = a[0].eval(vars), lo = a[1].eval(vars), hi = a[2].eval(vars)
                if x.isNaN || lo.isNaN || hi.isNaN || lo > hi { r = .nan } else { r = min(max(x, lo), hi) }
            default: die("expression function \(n) not ported")
            }
        }
        return sign * r
    }

    static func parse(_ s: String) -> Expr {
        var p = Array(s.utf8).filter { $0 != 0x20 }
        p.append(0)
        var i = 0
        func subexpr() -> Expr {
            var e0 = term()
            while p[i] == UInt8(ascii: "+") || p[i] == UInt8(ascii: "-") { e0 = Expr(.add, [e0, term()]) }
            return e0
        }
        func term() -> Expr {
            var e0 = factor()
            while p[i] == UInt8(ascii: "*") || p[i] == UInt8(ascii: "/") {
                let c = p[i]; i += 1
                e0 = Expr(c == UInt8(ascii: "*") ? .mul : .div, [e0, factor()])
            }
            return e0
        }
        func factor() -> Expr {
            var sign = 1.0
            if p[i] == UInt8(ascii: "+") { i += 1 } else if p[i] == UInt8(ascii: "-") { sign = -1; i += 1 }
            let e = primary()
            if p[i] == UInt8(ascii: "^") { die("'^' not ported") }
            e.sign *= sign
            return e
        }
        func primary() -> Expr {
            let c = p[i]
            if (c >= 0x30 && c <= 0x39) || c == UInt8(ascii: ".") {
                var j = i
                while (p[j] >= 0x30 && p[j] <= 0x39) || p[j] == UInt8(ascii: ".") { j += 1 }
                if p[j] == UInt8(ascii: "e") || p[j] == UInt8(ascii: "E") { die("exponent literal not ported") }
                let d = Double(String(decoding: p[i..<j], as: UTF8.self))!
                i = j
                return Expr(.num(d))
            }
            if c == UInt8(ascii: "(") {
                i += 1
                let e = subexpr()
                guard p[i] == UInt8(ascii: ")") else { die("missing ) in \(s)") }
                i += 1
                return e
            }
            var j = i
            while (p[j] >= 0x61 && p[j] <= 0x7a) || (p[j] >= 0x41 && p[j] <= 0x5a) || p[j] == UInt8(ascii: "_") { j += 1 }
            let name = String(decoding: p[i..<j], as: UTF8.self)
            if name.isEmpty { die("cannot parse expression \(s)") }
            i = j
            if p[i] != UInt8(ascii: "(") {
                if name == "PI" { return Expr(.num(Double.pi)) }
                return Expr(.v(name))
            }
            i += 1
            var args: [Expr] = [subexpr()]
            while p[i] == UInt8(ascii: ",") { i += 1; args.append(subexpr()) }
            guard p[i] == UInt8(ascii: ")") else { die("missing ) in \(s)") }
            i += 1
            return Expr(.fn(name), args)
        }
        let e = subexpr()
        if p[i] != 0 { die("trailing characters in expression \(s)") }
        return e
    }
}

func num(_ s: String) -> Double { Expr.parse(s).eval([:]) }

// AV_OPT_TYPE_DURATION as av_parse_time reads "S[.frac]": microseconds.
func durationUs(_ s: String) -> Int64 {
    let parts = s.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count <= 2, let whole = Int64(parts[0]) else { die("duration \(s) not ported") }
    var us = whole * 1_000_000
    if parts.count == 2 {
        var frac = String(parts[1].prefix(6))
        while frac.count < 6 { frac += "0" }
        us += Int64(frac)!
    }
    return us
}

// av_rescale_q with AV_ROUND_NEAR_INF, for the non-negative values used here.
func rescale(_ a: Int64, _ bnum: Int64, _ bden: Int64, _ cnum: Int64, _ cden: Int64) -> Int64 {
    let b = bnum * cden, c = cnum * bden
    return (a * b + c / 2) / c
}

// ---------------------------------------------------------------------------
// Filtergraph text -> chains of filters.
// ---------------------------------------------------------------------------
struct FSpec {
    let name: String
    var pos: [String] = []
    var kv: [String: String] = [:]
    var enable: String?
    func opt(_ k: String, _ i: Int? = nil) -> String? {
        if let v = kv[k] { return v }
        if let i = i, i < pos.count { return pos[i] }
        return nil
    }
}

struct Chain {
    var ins: [String]
    var filters: [FSpec]
    var outs: [String]
}

func splitTop(_ s: String, _ sep: Character) -> [String] {
    var out: [String] = [], cur = "", q = false
    for ch in s {
        if ch == "'" { q.toggle(); cur.append(ch); continue }
        if ch == sep && !q { out.append(cur); cur = ""; continue }
        cur.append(ch)
    }
    out.append(cur)
    return out
}

func unquote(_ s: String) -> String { s.replacingOccurrences(of: "'", with: "") }

func parseGraph(_ fc: String) -> [Chain] {
    var chains: [Chain] = []
    for part in splitTop(fc, ";") {
        var s = Substring(part)
        var ins: [String] = [], outs: [String] = []
        while s.hasPrefix("[") {
            let e = s.firstIndex(of: "]")!
            ins.append(String(s[s.index(after: s.startIndex)..<e]))
            s = s[s.index(after: e)...]
        }
        while s.hasSuffix("]") {
            let b = s.lastIndex(of: "[")!
            outs.insert(String(s[s.index(after: b)..<s.index(before: s.endIndex)]), at: 0)
            s = s[..<b]
        }
        var filters: [FSpec] = []
        for f in splitTop(String(s), ",") {
            let eq = f.firstIndex(of: "=")
            var spec = FSpec(name: String(eq.map { f[..<$0] } ?? Substring(f)))
            if let eq = eq {
                for arg in splitTop(String(f[f.index(after: eq)...]), ":") {
                    if let k = arg.firstIndex(of: "="), !arg[..<k].contains("'"), !arg[..<k].contains("(") {
                        let key = String(arg[..<k]), val = unquote(String(arg[arg.index(after: k)...]))
                        if key == "enable" { spec.enable = val } else { spec.kv[key] = val }
                    } else {
                        spec.pos.append(unquote(arg))
                    }
                }
            }
            filters.append(spec)
        }
        chains.append(Chain(ins: ins, filters: filters, outs: outs))
    }
    return chains
}

// ---------------------------------------------------------------------------
// Metal
// ---------------------------------------------------------------------------
let kernelSource = """
#include <metal_stdlib>
using namespace metal;
#define P(i) prm[i]

kernel void k_copy(device const uchar* s [[buffer(0)]], device uchar* d [[buffer(1)]],
                   constant int* prm [[buffer(2)]], uint2 g [[thread_position_in_grid]]) {
    // w, h, sstride, sx, sy, dstride, dx, dy
    int x = g.x, y = g.y; if (x >= P(0) || y >= P(1)) return;
    d[(y + P(7)) * P(5) + x + P(6)] = s[(y + P(4)) * P(2) + x + P(3)];
}
kernel void k_fill(device uchar* d [[buffer(0)]], constant int* prm [[buffer(1)]],
                   uint g [[thread_position_in_grid]]) {
    if ((int)g < P(0)) d[g] = uchar(P(1));
}
kernel void k_lut(device const uchar* s [[buffer(0)]], device uchar* d [[buffer(1)]],
                  device const uchar* lut [[buffer(2)]], constant int* prm [[buffer(3)]],
                  uint g [[thread_position_in_grid]]) {
    // 4 samples a thread
    int n = P(0), o = int(g) * 4; if (o >= n) return;
    if (o + 4 <= n) { uchar4 v = ((device const uchar4*)s)[g]; ((device uchar4*)d)[g] = uchar4(lut[v.x], lut[v.y], lut[v.z], lut[v.w]); }
    else for (int k = 0; o + k < n; k++) d[o + k] = lut[s[o + k]];
}

// --- swscale -------------------------------------------------------------
kernel void k_hscale8(device const uchar* s [[buffer(0)]], device short* d [[buffer(1)]],
                      device const int* fpos [[buffer(2)]], device const short* f [[buffer(3)]],
                      constant int* prm [[buffer(4)]], uint2 g [[thread_position_in_grid]]) {
    // srcW, dstW, H, fsize   (hScale8To15 / ff_hscale8to15_*_neon)
    int x = g.x, y = g.y; if (x >= P(1) || y >= P(2)) return;
    int fs = P(3), pos = fpos[x], v = 0;
    device const uchar* r = s + y * P(0);
    for (int j = 0; j < fs; j++) { int i = pos + j; if (i < P(0)) v += int(r[i]) * int(f[x * fs + j]); }
    d[y * P(1) + x] = short(min(v >> 7, 32767));
}
kernel void k_hscale16(device const ushort* s [[buffer(0)]], device short* d [[buffer(1)]],
                       device const int* fpos [[buffer(2)]], device const short* f [[buffer(3)]],
                       constant int* prm [[buffer(4)]], uint2 g [[thread_position_in_grid]]) {
    // srcW, dstW, H, fsize, sh   (hScale16To15)
    int x = g.x, y = g.y; if (x >= P(1) || y >= P(2)) return;
    int fs = P(3), pos = fpos[x], v = 0;
    device const ushort* r = s + y * P(0);
    for (int j = 0; j < fs; j++) { int i = pos + j; if (i < P(0)) v += int(r[i]) * int(f[x * fs + j]); }
    d[y * P(1) + x] = short(min(v >> P(4), 32767));
}
kernel void k_rgb2y(device const uchar* r [[buffer(0)]], device const uchar* gg [[buffer(1)]],
                    device const uchar* b [[buffer(2)]], device ushort* d [[buffer(3)]],
                    constant int* prm [[buffer(4)]], uint i [[thread_position_in_grid]]) {
    // n, ry, gy, by   (planar_rgb_to_y)
    if ((int)i >= P(0)) return;
    uint v = uint(P(1)) * uint(r[i]) + uint(P(2)) * uint(gg[i]) + uint(P(3)) * uint(b[i]) + (0x801u << 8);
    d[i] = ushort(as_type<int>(v) >> 9);
}
kernel void k_rgb2uv(device const uchar* r [[buffer(0)]], device const uchar* gg [[buffer(1)]],
                     device const uchar* b [[buffer(2)]], device ushort* du [[buffer(3)]],
                     device ushort* dv [[buffer(4)]], constant int* prm [[buffer(5)]],
                     uint2 g [[thread_position_in_grid]]) {
    // srcW, H, half, ru, gu, bu, rv, gv, bv   (planar_rgb_to_uv / gbr24pToUV_half_c)
    int x = g.x, y = g.y, hf = P(2);
    int ow = hf ? P(0) / 2 : P(0);
    if (x >= ow || y >= P(1)) return;
    int o = y * ow + x;
    if (!hf) {
        int i = y * P(0) + x;
        uint u = uint(P(3)) * uint(r[i]) + uint(P(4)) * uint(gg[i]) + uint(P(5)) * uint(b[i]) + (0x4001u << 8);
        uint v = uint(P(6)) * uint(r[i]) + uint(P(7)) * uint(gg[i]) + uint(P(8)) * uint(b[i]) + (0x4001u << 8);
        du[o] = ushort(as_type<int>(u) >> 9);
        dv[o] = ushort(as_type<int>(v) >> 9);
    } else {
        int i = y * P(0) + 2 * x;
        uint rr = uint(r[i]) + uint(r[i + 1]), g2 = uint(gg[i]) + uint(gg[i + 1]), bb = uint(b[i]) + uint(b[i + 1]);
        uint u = uint(P(3)) * rr + uint(P(4)) * g2 + uint(P(5)) * bb + (0x4001u << 9);
        uint v = uint(P(6)) * rr + uint(P(7)) * g2 + uint(P(8)) * bb + (0x4001u << 9);
        du[o] = ushort(u >> 10);
        dv[o] = ushort(v >> 10);
    }
}
kernel void k_lumtojpeg(device short* d [[buffer(0)]], constant int* prm [[buffer(1)]],
                        uint i [[thread_position_in_grid]]) {
    // n, coeff, offset   (ff_lumRangeToJpeg8_neon: 32-bit mla, sqshrn #14)
    if ((int)i >= P(0)) return;
    int v = int(d[i]) * P(1) + P(2);
    d[i] = short(clamp(v >> 14, -32768, 32767));
}
kernel void k_vplane(device const short* s [[buffer(0)]], device uchar* d [[buffer(1)]],
                     device const int* fpos [[buffer(2)]], device const short* f [[buffer(3)]],
                     constant int* prm [[buffer(4)]], uint2 g [[thread_position_in_grid]]) {
    // W, dstH, fsize   (yuv2planeX_8 / yuv2plane1_8, dither 64)
    int x = g.x, y = g.y; if (x >= P(0) || y >= P(1)) return;
    int fs = P(2), pos = fpos[y], w = P(0);
    if (fs == 1) { d[y * w + x] = uchar(clamp((int(s[pos * w + x]) + 64) >> 7, 0, 255)); return; }
    int v = 64 << 12;
    for (int j = 0; j < fs; j++) v += int(s[(pos + j) * w + x]) * int(f[y * fs + j]);
    d[y * w + x] = uchar(clamp(v >> 19, 0, 255));
}
inline int clipu30(int a) { return (a & ~((1 << 30) - 1)) ? (((~a) >> 31) & ((1 << 30) - 1)) : a; }
kernel void k_vgbrp(device const short* Y [[buffer(0)]], device const short* U [[buffer(1)]],
                    device const short* V [[buffer(2)]], device uchar* R [[buffer(3)]],
                    device uchar* G [[buffer(4)]], device uchar* B [[buffer(5)]],
                    device const int* lpos [[buffer(6)]], device const short* lf [[buffer(7)]],
                    device const int* cpos [[buffer(8)]], device const short* cf [[buffer(9)]],
                    constant int* prm [[buffer(10)]], uint2 g [[thread_position_in_grid]]) {
    // W, H, lfs, cfs, yoff, ycoef, v2r, v2g, u2g, u2b   (yuv2gbrp_full_X_c)
    int x = g.x, y = g.y, w = P(0); if (x >= w || y >= P(1)) return;
    int lfs = P(2), cfs = P(3), lp = lpos[y], cp = cpos[y];
    int yy = 1 << 9, u = (1 << 9) - (128 << 19), v = u;
    for (int j = 0; j < lfs; j++) yy += int(Y[(lp + j) * w + x]) * int(lf[y * lfs + j]);
    for (int j = 0; j < cfs; j++) {
        u += int(U[(cp + j) * w + x]) * int(cf[y * cfs + j]);
        v += int(V[(cp + j) * w + x]) * int(cf[y * cfs + j]);
    }
    yy >>= 10; u >>= 10; v >>= 10;
    yy -= P(4); yy *= P(5); yy += 1 << 21;
    int r = yy + v * P(6), gg = yy + v * P(7) + u * P(8), b = yy + u * P(9);
    if ((r | gg | b) & 0xC0000000) { r = clipu30(r); gg = clipu30(gg); b = clipu30(b); }
    int o = y * w + x;
    R[o] = uchar(r >> 22); G[o] = uchar(gg >> 22); B[o] = uchar(b >> 22);
}
kernel void k_yuv2rgb_tab(device const uchar* Yp [[buffer(0)]], device const uchar* Up [[buffer(1)]],
                          device const uchar* Vp [[buffer(2)]], device uchar* R [[buffer(3)]],
                          device uchar* G [[buffer(4)]], device uchar* B [[buffer(5)]],
                          device const uchar* ytab [[buffer(6)]], device const int* t [[buffer(7)]],
                          constant int* prm [[buffer(8)]], uint2 g [[thread_position_in_grid]]) {
    // W, H, chroma W   (yuv2rgb_c_24_rgb / yuv420p_gbrp_c tables)
    int x = g.x, y = g.y; if (x >= P(0) || y >= P(1)) return;
    int c = (y >> 1) * P(2) + (x >> 1), o = y * P(0) + x;
    int u = Up[c], v = Vp[c], yy = Yp[o];
    R[o] = ytab[t[v] + yy];
    G[o] = ytab[t[256 + u] + t[512 + v] + yy];
    B[o] = ytab[t[768 + u] + yy];
}

// ff_yuv420p_to_{rgb24,gbrp,...}_neon: width % 16 == 0, even height
inline short sqdmulh(int a, int b) { return short(min((a * b) >> 15, 32767)); }
kernel void k_yuv2rgb_neon(device const uchar* Yp [[buffer(0)]], device const uchar* Up [[buffer(1)]],
                           device const uchar* Vp [[buffer(2)]], device uchar* R [[buffer(3)]],
                           device uchar* G [[buffer(4)]], device uchar* B [[buffer(5)]],
                           constant int* prm [[buffer(6)]], uint2 g [[thread_position_in_grid]]) {
    // W, H, chroma W, y_offset >> 6, y_coeff, v2r, u2g, v2g, u2b
    int x = g.x, y = g.y; if (x >= P(0) || y >= P(1)) return;
    int c = (y >> 1) * P(2) + (x >> 1), o = y * P(0) + x;
    short u = short(int(Up[c]) << 3) - short(1024), v = short(int(Vp[c]) << 3) - short(1024);
    short yy = sqdmulh(short(short(int(Yp[o]) << 3) - short(P(3))), P(4));
    short rc = sqdmulh(v, P(5)), gc = short(sqdmulh(u, P(6)) + sqdmulh(v, P(7))), bc = sqdmulh(u, P(8));
    // sqrshrun #1 of the wrapping 16-bit sums
    R[o] = uchar(clamp((int(short(yy + rc)) + 1) >> 1, 0, 255));
    G[o] = uchar(clamp((int(short(yy + gc)) + 1) >> 1, 0, 255));
    B[o] = uchar(clamp((int(short(yy + bc)) + 1) >> 1, 0, 255));
}

// --- eq / colorbalance / vignette ---------------------------------------
kernel void k_cbal(device const uchar* R [[buffer(0)]], device const uchar* G [[buffer(1)]],
                   device const uchar* B [[buffer(2)]], device uchar* oR [[buffer(3)]],
                   device uchar* oG [[buffer(4)]], device uchar* oB [[buffer(5)]],
                   device const float* vl [[buffer(6)]], device const float* smh [[buffer(7)]],
                   constant int* prm [[buffer(8)]], uint i [[thread_position_in_grid]]) {
    if ((int)i >= P(0)) return;
    uchar c[3] = { R[i], G[i], B[i] };
    uchar mx = max(max(c[0], c[1]), c[2]), mn = min(min(c[0], c[1]), c[2]);
    int k = (int(mx) * 256 + int(mn)) * 9;
    uchar o[3];
    for (int ch = 0; ch < 3; ch++) {
        float v = vl[c[ch]];
        v = v + smh[k + ch * 3 + 0];
        v = v + smh[k + ch * 3 + 1];
        v = v + smh[k + ch * 3 + 2];
        v = clamp(v, 0.0f, 1.0f);
        o[ch] = uchar(clamp(rint(v * 255.0f), 0.0f, 255.0f));
    }
    oR[i] = o[0]; oG[i] = o[1]; oB[i] = o[2];
}
kernel void k_vign(device const uchar* R [[buffer(0)]], device const uchar* G [[buffer(1)]],
                   device const uchar* B [[buffer(2)]], device uchar* oR [[buffer(3)]],
                   device uchar* oG [[buffer(4)]], device uchar* oB [[buffer(5)]],
                   device const float* fmap [[buffer(6)]], device const uint* A [[buffer(7)]],
                   device const uint* C [[buffer(8)]], constant uint* prm [[buffer(9)]],
                   uint i [[thread_position_in_grid]]) {
    // n, dither state at this frame's first sample
    if (i >= prm[0]) return;
    float f = fmap[i];
    uchar s[3] = { R[i], G[i], B[i] };
    uchar o[3];
    for (int c = 0; c < 3; c++) {
        uint st = A[i * 3 + c] * prm[1] + C[i * 3 + c];
        // av_clip_uint8(float(src * f) + st / 2^32), the sum exact in double
        float p = float(s[c]) * f;
        float fl = floor(p);
        float F = (p - fl) * 4294967296.0f;
        uint Fi = uint(floor(F));
        int carry = (st != 0u && Fi >= (0u - st)) ? 1 : 0;
        o[c] = uchar(clamp(int(fl) + carry, 0, 255));
    }
    oR[i] = o[0]; oG[i] = o[1]; oB[i] = o[2];
}

// --- blend / overlay / alpha --------------------------------------------
// x / 255 for 0 <= x <= 255 * 255 (checked over the whole range)
inline int4 div255(int4 x) { return (x * 32897) >> 23; }
kernel void k_blend(device const uchar* a [[buffer(0)]], device const uchar* b [[buffer(1)]],
                    device uchar* d [[buffer(2)]], constant int* prm [[buffer(3)]],
                    uint i [[thread_position_in_grid]]) {
    // n, mode (0 multiply, 1 screen, 2 addition); 4 samples a thread
    int n = P(0), o = int(i) * 4; if (o >= n) return;
    int4 A, Bv;
    if (o + 4 <= n) { A = int4(((device const uchar4*)a)[i]); Bv = int4(((device const uchar4*)b)[i]); }
    else { A = 0; Bv = 0; for (int k = 0; o + k < n; k++) { A[k] = a[o + k]; Bv[k] = b[o + k]; } }
    int4 r;
    if (P(1) == 0) r = div255(A * Bv);
    else if (P(1) == 1) r = 255 - div255((255 - A) * (255 - Bv));
    else r = min(255, A + Bv);
    if (o + 4 <= n) ((device uchar4*)d)[i] = uchar4(r);
    else for (int k = 0; o + k < n; k++) d[o + k] = uchar(r[k]);
}
inline int fdiv255(int x) { return ((x + 128) * 257) >> 16; }
inline int unpre(int x, int y) {
    return uchar((((x) << 16) - ((x) << 9) + (x)) / ((((x) + (y)) << 8) - ((x) + (y)) - (y) * (x)));
}
kernel void k_ovl_rgba(device uchar* dR [[buffer(0)]], device uchar* dG [[buffer(1)]],
                       device uchar* dB [[buffer(2)]], device uchar* dA [[buffer(3)]],
                       device const uchar* sR [[buffer(4)]], device const uchar* sG [[buffer(5)]],
                       device const uchar* sB [[buffer(6)]], device const uchar* sA [[buffer(7)]],
                       device const uchar* mA [[buffer(8)]],
                       constant int* prm [[buffer(9)]], uint2 g [[thread_position_in_grid]]) {
    // dW, dH, sW, sH, x, y   (blend_slice_packed_rgb, main + overlay straight)
    int j = g.x, i = g.y;
    if (j >= P(2) || i >= P(3)) return;
    int X = P(4) + j, Y = P(5) + i;
    if (X < 0 || X >= P(0) || Y < 0 || Y >= P(1) || i >= min(P(3), P(1))) return;
    int s = i * P(2) + j, o = Y * P(0) + X;
    int alpha = sA[s];
    int ad = mA[o];
    if (alpha != 0 && alpha != 255) alpha = unpre(alpha, ad);
    if (alpha == 255) { dR[o] = sR[s]; dG[o] = sG[s]; dB[o] = sB[s]; }
    else if (alpha != 0) {
        dR[o] = uchar(fdiv255(int(dR[o]) * (255 - alpha) + int(sR[s]) * alpha));
        dG[o] = uchar(fdiv255(int(dG[o]) * (255 - alpha) + int(sG[s]) * alpha));
        dB[o] = uchar(fdiv255(int(dB[o]) * (255 - alpha) + int(sB[s]) * alpha));
    }
    if (alpha == 255) dA[o] = sA[s];
    else if (alpha != 0) dA[o] = uchar(ad + fdiv255((255 - ad) * int(sA[s])));
}
kernel void k_ovl_yuva_plane(device uchar* d [[buffer(0)]], device const uchar* s [[buffer(1)]],
                             device const uchar* a [[buffer(2)]], device const uchar* da [[buffer(3)]],
                             constant int* prm [[buffer(4)]], uint2 g [[thread_position_in_grid]]) {
    // dWp, dHp, sWp, sHp, xp, yp, sub, sW(luma), dW(luma)   (blend_plane_8_8bits, straight/straight)
    int k = g.x, j = g.y;
    int dwp = P(0), dhp = P(1), swp = P(2), shp = P(3), xp = P(4), yp = P(5), sub = P(6), sw = P(7), dw = P(8);
    int jmin = max(-yp, 0), jmax = min(min(-yp + dhp, min(shp, dhp)), yp + shp);
    int kmin = max(-xp, 0), kmax = min(-xp + dwp, swp);
    if (j < jmin || j >= jmax || k < kmin || k >= kmax) return;
    int ai = (j << sub) * sw + (k << sub);
    int alpha;
    if (sub && j + 1 < shp && k + 1 < swp) alpha = (a[ai] + a[ai + sw] + a[ai + 1] + a[ai + sw + 1]) >> 2;
    else if (sub) {
        int ah = k + 1 < swp ? (a[ai] + a[ai + 1]) >> 1 : a[ai];
        int av = j + 1 < shp ? (a[ai] + a[ai + sw]) >> 1 : a[ai];
        alpha = (av + ah) >> 1;
    } else alpha = a[ai];
    if (alpha != 0 && alpha != 255) {
        int di = ((yp + j) << sub) * dw + ((xp + k) << sub);
        int ad;
        if (sub && j + 1 < shp && k + 1 < swp) ad = (da[di] + da[di + dw] + da[di + 1] + da[di + dw + 1]) >> 2;
        else if (sub) {
            int ah = k + 1 < swp ? (da[di] + da[di + 1]) >> 1 : da[di];
            int av = j + 1 < shp ? (da[di] + da[di + dw]) >> 1 : da[di];
            ad = (av + ah) >> 1;
        } else ad = da[di];
        alpha = unpre(alpha, ad);
    }
    int o = (yp + j) * dwp + xp + k;
    d[o] = uchar(fdiv255(int(d[o]) * (255 - alpha) + int(s[j * swp + k]) * alpha));
}
kernel void k_alpha_comp(device uchar* d [[buffer(0)]], device const uchar* s [[buffer(1)]],
                         device const uchar* m [[buffer(2)]], constant int* prm [[buffer(3)]],
                         uint2 g [[thread_position_in_grid]]) {
    // dW, dH, sW, sH, x, y   (alpha_composite_8_8bits, main straight)
    int jj = g.x, ii = g.y;
    int imin = max(-P(5), 0), imax = min(min(-P(5) + P(1), min(P(3), P(1))), P(5) + P(3));
    int jmin = max(-P(4), 0), jmax = min(-P(4) + P(0), P(2));
    if (ii < imin || ii >= imax || jj < jmin || jj >= jmax) return;
    int o = (P(5) + ii) * P(0) + P(4) + jj;
    int alpha = s[ii * P(2) + jj];
    int ad = m[o];
    if (alpha != 0 && alpha != 255) alpha = unpre(alpha, ad);
    if (alpha == 255) d[o] = s[ii * P(2) + jj];
    else if (alpha > 0) d[o] = uchar(ad + fdiv255((255 - ad) * int(s[ii * P(2) + jj])));
}

// --- gblur ----------------------------------------------------------------
kernel void k_g_load(device const uchar* s [[buffer(0)]], device float* b [[buffer(1)]],
                     constant int* prm [[buffer(2)]], uint2 g [[thread_position_in_grid]]) {
    // w, h: loads transposed, so the row pass walks memory the threads share
    int w = P(0), h = P(1); if ((int)g.x >= w || (int)g.y >= h) return;
    b[g.x * h + g.y] = float(s[g.y * w + g.x]);
}
kernel void k_g_tr(device const float* s [[buffer(0)]], device float* d [[buffer(1)]],
                   constant int* prm [[buffer(2)]], uint2 g [[thread_position_in_grid]]) {
    int w = P(0), h = P(1); if ((int)g.x >= w || (int)g.y >= h) return;
    d[g.y * w + g.x] = s[g.x * h + g.y];
}
// One recursive pass along a line of n samples `stride` apart; the line's
// first sample is p[0]. horiz_slice_c / do_vertical_columns, `+=` contracted
// to fma; the running value stays in a register, the arithmetic is unchanged.
inline void g_line(device float* p, int n, int stride, int steps, float nu, float bs) {
    // loads go out 8 at a time ahead of the dependent chain: there are too
    // few lines in flight to hide their latency otherwise
    for (int st = 0; st < steps; st++) {
        float v = p[0] * bs; p[0] = v;
        int i = 1;
        for (; i + 8 <= n; i += 8) {
            float c[8];
            for (int k = 0; k < 8; k++) c[k] = p[(i + k) * stride];
            for (int k = 0; k < 8; k++) { v = fma(nu, v, c[k]); p[(i + k) * stride] = v; }
        }
        for (; i < n; i++) { v = fma(nu, v, p[i * stride]); p[i * stride] = v; }
        v *= bs; p[(n - 1) * stride] = v;
        i = n - 2;
        for (; i - 7 >= 0; i -= 8) {
            float c[8];
            for (int k = 0; k < 8; k++) c[k] = p[(i - k) * stride];
            for (int k = 0; k < 8; k++) { v = fma(nu, v, c[k]); p[(i - k) * stride] = v; }
        }
        for (; i >= 0; i--) { v = fma(nu, v, p[i * stride]); p[i * stride] = v; }
    }
}
kernel void k_g_h(device float* b [[buffer(0)]], constant int* prm [[buffer(1)]],
                  constant float* fp [[buffer(2)]], uint y [[thread_position_in_grid]]) {
    // w, h, steps ; nu, bscale   on the transposed buffer
    if ((int)y >= P(1)) return;
    g_line(b + y, P(0), P(1), P(2), fp[0], fp[1]);
}
kernel void k_g_v(device float* b [[buffer(0)]], constant int* prm [[buffer(1)]],
                  constant float* fp [[buffer(2)]], uint x [[thread_position_in_grid]]) {
    if ((int)x >= P(0)) return;
    g_line(b + x, P(1), P(0), P(2), fp[0], fp[1]);
}
kernel void k_g_store(device const float* b [[buffer(0)]], device uchar* d [[buffer(1)]],
                      constant int* prm [[buffer(2)]], constant float* fp [[buffer(3)]],
                      uint i [[thread_position_in_grid]]) {
    if ((int)i >= P(0)) return;
    float v = b[i] * fp[0];
    v = clamp(v, 0.0f, 255.0f);
    d[i] = uchar(rint(v));
}

// --- the rest ---------------------------------------------------------------
kernel void k_fade_a(device const uchar* s [[buffer(0)]], device uchar* d [[buffer(1)]],
                     constant int* prm [[buffer(2)]], uint i [[thread_position_in_grid]]) {
    // n, factor, black level, scaled black level
    if ((int)i < P(0)) d[i] = uchar(((int(s[i]) - P(2)) * P(1) + P(3)) >> 16);
}
kernel void k_fade_c(device const uchar* s [[buffer(0)]], device uchar* d [[buffer(1)]],
                     constant int* prm [[buffer(2)]], uint i [[thread_position_in_grid]]) {
    if ((int)i < P(0)) d[i] = uchar(((int(s[i]) - 128) * P(1) + 8421367) >> 16);
}
kernel void k_mixer(device const uchar* R [[buffer(0)]], device const uchar* G [[buffer(1)]],
                    device const uchar* B [[buffer(2)]], device uchar* oR [[buffer(3)]],
                    device uchar* oG [[buffer(4)]], device uchar* oB [[buffer(5)]],
                    device const int* L [[buffer(6)]], constant int* prm [[buffer(7)]],
                    uint i [[thread_position_in_grid]]) {
    if ((int)i >= P(0)) return;
    int r = R[i], g = G[i], b = B[i];
    oR[i] = uchar(clamp(L[r] + L[256 + g] + L[512 + b], 0, 255));
    oG[i] = uchar(clamp(L[768 + r] + L[1024 + g] + L[1280 + b], 0, 255));
    oB[i] = uchar(clamp(L[1536 + r] + L[1792 + g] + L[2048 + b], 0, 255));
}
kernel void k_scroll(device const uchar* s [[buffer(0)]], device uchar* d [[buffer(1)]],
                     constant int* prm [[buffer(2)]], uint2 g [[thread_position_in_grid]]) {
    // w, h, pos_v, pos_h
    int x = g.x, y = g.y; if (x >= P(0) || y >= P(1)) return;
    d[y * P(0) + x] = s[((y + P(2)) % P(1)) * P(0) + (x + P(3)) % P(0)];
}
kernel void k_displace(device const uchar* s [[buffer(0)]], device const uchar* xm [[buffer(1)]],
                       device const uchar* ym [[buffer(2)]], device uchar* d [[buffer(3)]],
                       constant int* prm [[buffer(4)]], uint2 g [[thread_position_in_grid]]) {
    int x = g.x, y = g.y, w = P(0), h = P(1); if (x >= w || y >= h) return;
    int o = y * w + x;
    int Y = clamp(y + int(ym[o]) - 128, 0, h - 1), X = clamp(x + int(xm[o]) - 128, 0, w - 1);
    d[o] = s[Y * w + X];
}
kernel void k_nv12(device const uchar* U [[buffer(0)]], device const uchar* V [[buffer(1)]],
                   device uchar* d [[buffer(2)]], constant int* prm [[buffer(3)]],
                   uint i [[thread_position_in_grid]]) {
    if ((int)i < P(0)) { d[2 * i] = U[i]; d[2 * i + 1] = V[i]; }
}
"""

let dev: MTLDevice = MTLCreateSystemDefaultDevice() ?? { die("no Metal device") }()
let queue = dev.makeCommandQueue()!
let library: MTLLibrary = {
    let o = MTLCompileOptions()
    o.mathMode = .safe
    o.mathFloatingPointFunctions = .precise
    do { return try dev.makeLibrary(source: kernelSource, options: o) } catch { die("Metal compile: \(error)") }
}()
var pipelines: [String: MTLComputePipelineState] = [:]
func pipeline(_ name: String) -> MTLComputePipelineState {
    if let p = pipelines[name] { return p }
    let p = try! dev.makeComputePipelineState(function: library.makeFunction(name: name)!)
    pipelines[name] = p
    return p
}

func buf(_ n: Int) -> MTLBuffer { dev.makeBuffer(length: max(n, 16), options: .storageModeShared)! }
// tables: written once on the CPU, only ever read by kernels
var tables = Set<ObjectIdentifier>()
func bufOf<T>(_ a: [T]) -> MTLBuffer {
    let b = a.withUnsafeBytes { dev.makeBuffer(bytes: $0.baseAddress!, length: max($0.count, 16), options: .storageModeShared)! }
    tables.insert(ObjectIdentifier(b))
    return b
}

final class GPU {
    var cb: MTLCommandBuffer!
    var enc: MTLComputeCommandEncoder!
    var dispatches = 0, barriers = 0
    // A concurrent encoder, with a barrier only before a dispatch that binds
    // an image buffer some dispatch since the last barrier bound: independent
    // work overlaps, dependent work stays in order.
    var touched = Set<ObjectIdentifier>()
    func begin() {
        cb = queue.makeCommandBuffer()!; touched = []
        enc = prof ? nil : cb.makeComputeCommandEncoder(dispatchType: .concurrent)!
    }
    func end() -> MTLCommandBuffer {
        if !prof { enc.endEncoding() }
        cb.commit(); let c = cb!; cb = nil; enc = nil
        if prof {
            c.waitUntilCompleted()
            let d = try! sampler!.resolveCounterRange(0..<(2 * profNames.count))!
            d.withUnsafeBytes { (r: UnsafeRawBufferPointer) in
                let t = r.bindMemory(to: MTLCounterResultTimestamp.self)
                for (i, k) in profNames.enumerated() { profT[k, default: 0] += Double(t[2 * i + 1].timestamp - t[2 * i].timestamp) * 1e-9 }
            }
            profNames = []
        }
        return c
    }
    // BARS_GPU_PROF: every dispatch in an encoder of its own, timed by the GPU's
    // timestamp counter (ns on Apple GPUs)
    let prof = ProcessInfo.processInfo.environment["BARS_GPU_PROF"] != nil
    var profT: [String: Double] = [:], profNames: [String] = []
    lazy var sampler: MTLCounterSampleBuffer? = {
        let d = MTLCounterSampleBufferDescriptor()
        d.counterSet = dev.counterSets!.first { $0.name == MTLCommonCounterSet.timestamp.rawValue }!
        d.sampleCount = 4096; d.storageMode = .shared
        return try! dev.makeCounterSampleBuffer(descriptor: d)
    }()
    // buffers in order, then the int params (and optional float params) as the next slots
    func run(_ name: String, _ bufs: [MTLBuffer], _ ip: [Int32], fp: [Float]? = nil, grid: (Int, Int), tg: Int? = nil) {
        let ps = pipeline(name)
        if prof {
            let pd = MTLComputePassDescriptor()
            pd.sampleBufferAttachments[0].sampleBuffer = sampler!
            pd.sampleBufferAttachments[0].startOfEncoderSampleIndex = 2 * profNames.count
            pd.sampleBufferAttachments[0].endOfEncoderSampleIndex = 2 * profNames.count + 1
            enc = cb.makeComputeCommandEncoder(descriptor: pd)!
            profNames.append("\(name) \(grid.0)x\(grid.1)")
        }
        enc.setComputePipelineState(ps)
        let ids = bufs.map { ObjectIdentifier($0) }.filter { !tables.contains($0) }
        if ids.contains(where: touched.contains) { enc.memoryBarrier(scope: .buffers); touched = []; barriers += 1 }
        touched.formUnion(ids)
        for (i, b) in bufs.enumerated() { enc.setBuffer(b, offset: 0, index: i) }
        var ipv = ip.isEmpty ? [0] : ip
        enc.setBytes(&ipv, length: ipv.count * 4, index: bufs.count)
        if var f = fp { enc.setBytes(&f, length: f.count * 4, index: bufs.count + 1) }
        let tw = tg ?? (grid.1 == 1 ? min(256, ps.maxTotalThreadsPerThreadgroup) : 16)
        let th = grid.1 == 1 ? 1 : max(1, min(16, ps.maxTotalThreadsPerThreadgroup / 16))
        enc.dispatchThreads(MTLSize(width: grid.0, height: grid.1, depth: 1),
                            threadsPerThreadgroup: MTLSize(width: tw, height: th, depth: 1))
        dispatches += 1
        if prof { enc.endEncoding() }
    }
}
let gpu = GPU()

// ---------------------------------------------------------------------------
// Images: planar u8 buffers. rgb/rgba are stored R,G,B(,A); dumps use
// ffmpeg's gbrp/gbrap plane order.
// ---------------------------------------------------------------------------
enum PF: String { case yuv420, yuva420, rgb, rgba, gray }

struct Meta {
    var range = 0          // 0 tv/unspecified, 1 pc
    var csp = 2            // AVColorSpace: 0 rgb, 1 bt709, 2 unspecified
    var hloc = 128         // av_chroma_location_enum_to_pos x (0 left, 128 center)
    var vloc = 128
    var sar = (1, 1)
}

final class Img {
    let pf: PF, w: Int, h: Int
    var p: [MTLBuffer]
    var meta: Meta
    init(_ pf: PF, _ w: Int, _ h: Int, _ meta: Meta, planes: [MTLBuffer]? = nil) {
        self.pf = pf; self.w = w; self.h = h; self.meta = meta
        p = planes ?? (0..<Img.nplanes(pf)).map { i in
            let (pw, ph) = Img.dims(pf, w, h, i)
            return buf(pw * ph)
        }
    }
    static func nplanes(_ pf: PF) -> Int {
        switch pf { case .yuv420, .rgb: return 3; case .yuva420, .rgba: return 4; case .gray: return 1 }
    }
    static func dims(_ pf: PF, _ w: Int, _ h: Int, _ i: Int) -> (Int, Int) {
        if (pf == .yuv420 || pf == .yuva420) && (i == 1 || i == 2) { return ((w + 1) >> 1, (h + 1) >> 1) }
        return (w, h)
    }
    func dims(_ i: Int) -> (Int, Int) { Img.dims(pf, w, h, i) }
    var ffName: String {
        switch pf { case .yuv420: return "yuv420p"; case .yuva420: return "yuva420p"; case .rgb: return "gbrp"
        case .rgba: return "gbrap"; case .gray: return "gray" }
    }
    var dumpOrder: [Int] { (pf == .rgb || pf == .rgba) ? (pf == .rgb ? [1, 2, 0] : [1, 2, 0, 3]) : Array(0..<p.count) }
}

func copyPlane(_ s: MTLBuffer, _ d: MTLBuffer, w: Int, h: Int, sstride: Int, sx: Int = 0, sy: Int = 0,
               dstride: Int, dx: Int = 0, dy: Int = 0) {
    gpu.run("k_copy", [s, d], [w, h, sstride, sx, sy, dstride, dx, dy].map { Int32($0) }, grid: (w, h))
}
func fillPlane(_ d: MTLBuffer, _ n: Int, _ v: Int) { gpu.run("k_fill", [d], [Int32(n), Int32(v)], grid: (n, 1)) }
func copyImg(_ s: Img, _ d: Img) {
    for i in 0..<min(s.p.count, d.p.count) { let (w, h) = s.dims(i); copyPlane(s.p[i], d.p[i], w: w, h: h, sstride: w, dstride: w) }
}

// ---------------------------------------------------------------------------
// swscale (legacy backend, arm64): initFilter + the scaled and unscaled paths.
// ---------------------------------------------------------------------------
let SWS_BILINEAR = 2, SWS_BICUBIC = 4, SWS_POINT = 0x10, SWS_AREA = 0x20, SWS_LANCZOS = 0x200

func swsFlag(_ s: String) -> Int {
    switch s { case "bilinear": return SWS_BILINEAR; case "bicubic": return SWS_BICUBIC; case "neighbor": return SWS_POINT
    case "area": return SWS_AREA; case "lanczos": return SWS_LANCZOS; default: die("scale flags \(s) not ported") }
}

func avlog2(_ v: Int) -> Int { v <= 0 ? 0 : (Int.bitWidth - 1 - v.leadingZeroBitCount) }
func roundedDiv(_ a: Int64, _ b: Int64) -> Int64 { (a >= 0 ? a + (b >> 1) : a - (b >> 1)) / b }

struct SwsFilter { var pos: [Int32]; var coef: [Int16]; var size: Int }

func initFilter(xInc: Int64, srcW: Int, dstW: Int, filterAlign alignIn: Int, one: Int64, scaler: Int,
                srcPos: Int, dstPos: Int) -> SwsFilter {
    var filterAlign = alignIn
    var filterSize: Int
    let fone = Int64(1) << (54 - min(avlog2(srcW / dstW), 8))
    var fpos = [Int](repeating: 0, count: dstW)
    var filter: [Int64]
    if abs(xInc - 0x10000) < 10 && srcPos == dstPos {
        filterSize = 1
        filter = [Int64](repeating: fone, count: dstW)
        for i in 0..<dstW { fpos[i] = i }
    } else if scaler == SWS_POINT {
        filterSize = 1
        filter = [Int64](repeating: fone, count: dstW)
        var xDstInSrc = ((Int64(dstPos) * xInc) >> 8) - ((Int64(srcPos) * 0x8000) >> 7)
        for i in 0..<dstW {
            fpos[i] = Int((xDstInSrc - Int64((filterSize - 1) << 15) + (1 << 15)) >> 16)
            xDstInSrc += xInc
        }
    } else if xInc <= (1 << 16) && scaler == SWS_AREA {
        filterSize = 2
        filter = [Int64](repeating: 0, count: dstW * 2)
        var xDstInSrc = ((Int64(dstPos) * xInc) >> 8) - ((Int64(srcPos) * 0x8000) >> 7)
        for i in 0..<dstW {
            var xx = Int((xDstInSrc - Int64((filterSize - 1) << 15) + (1 << 15)) >> 16)
            fpos[i] = xx
            for j in 0..<filterSize {
                var coeff = fone - abs(Int64(xx) * (1 << 16) - xDstInSrc) * (fone >> 16)
                if coeff < 0 { coeff = 0 }
                filter[i * filterSize + j] = coeff
                xx += 1
            }
            xDstInSrc += xInc
        }
    } else {
        var sizeFactor: Int
        switch scaler {
        case SWS_AREA: sizeFactor = 1
        case SWS_BICUBIC: sizeFactor = 4
        case SWS_BILINEAR: sizeFactor = 2
        case SWS_LANCZOS: sizeFactor = 6
        default: die("scaler \(scaler) not ported")
        }
        if xInc <= 1 << 16 { filterSize = 1 + sizeFactor } else { filterSize = 1 + (sizeFactor * srcW + dstW - 1) / dstW }
        filterSize = max(min(filterSize, srcW - 2), 1)
        filter = [Int64](repeating: 0, count: dstW * filterSize)
        var xDstInSrc = ((Int64(dstPos) * xInc) >> 7) - ((Int64(srcPos) * 0x10000) >> 7)
        for i in 0..<dstW {
            var xx = Int((xDstInSrc - Int64(filterSize - 2) * (1 << 16)) / (1 << 17))
            fpos[i] = xx
            for j in 0..<filterSize {
                var d = abs(Int64(xx) * (1 << 17) - xDstInSrc) << 13
                if xInc > 1 << 16 { d = d * Int64(dstW) / Int64(srcW) }
                let floatd = Double(d) * (1.0 / Double(1 << 30))
                var coeff: Int64
                switch scaler {
                case SWS_BICUBIC:
                    let B: Int64 = 0, C = Int64(0.6 * Double(1 << 24))
                    if d >= 1 << 31 { coeff = 0 } else {
                        let dd = (d * d) >> 30, ddd = (dd * d) >> 30
                        if d < 1 << 30 {
                            coeff = (12 * (1 << 24) - 9 * B - 6 * C) * ddd + (-18 * (1 << 24) + 12 * B + 6 * C) * dd
                                + (6 * (1 << 24) - 2 * B) * (1 << 30)
                        } else {
                            coeff = (-B - 6 * C) * ddd + (6 * B + 30 * C) * dd + (-12 * B - 48 * C) * d + (8 * B + 24 * C) * (1 << 30)
                        }
                    }
                    coeff /= (Int64(1) << 54) / fone
                case SWS_AREA:
                    let d2 = d - (1 << 29)
                    if d2 * xInc < -(Int64(1) << (29 + 16)) { coeff = Int64(1) << (30 + 16) }
                    else if d2 * xInc < Int64(1) << (29 + 16) { coeff = -d2 * xInc + (Int64(1) << (29 + 16)) }
                    else { coeff = 0 }
                    coeff *= fone >> (30 + 16)
                case SWS_LANCZOS:
                    let p = 3.0
                    var c = d != 0 ? sin(floatd * Double.pi) * sin(floatd * Double.pi / p) / (floatd * floatd * Double.pi * Double.pi / p) : 1.0
                    c = c * Double(fone)
                    coeff = Int64(c)
                    if floatd > p { coeff = 0 }
                default: // bilinear
                    coeff = (1 << 30) - d
                    if coeff < 0 { coeff = 0 }
                    coeff *= fone >> 30
                }
                filter[i * filterSize + j] = coeff
                xx += 1
            }
            xDstInSrc += 2 * xInc
        }
    }
    // no src/dst filter vectors: filter2 == filter
    let filter2Size = filterSize
    var filter2 = filter
    var minFilterSize = 0
    let cut = 0.002 * Double(fone)
    for i in stride(from: dstW - 1, through: 0, by: -1) {
        var mn = filter2Size
        var cutOff: Int64 = 0
        for _ in 0..<filter2Size {
            cutOff += abs(filter2[i * filter2Size])
            if Double(cutOff) > cut { break }
            if i < dstW - 1 && fpos[i] >= fpos[i + 1] { break }
            for k in 1..<max(filter2Size, 1) { filter2[i * filter2Size + k - 1] = filter2[i * filter2Size + k] }
            filter2[i * filter2Size + filter2Size - 1] = 0
            fpos[i] += 1
        }
        cutOff = 0
        var j = filter2Size - 1
        while j > 0 {
            cutOff += abs(filter2[i * filter2Size + j])
            if Double(cutOff) > cut { break }
            mn -= 1
            j -= 1
        }
        if mn > minFilterSize { minFilterSize = mn }
    }
    if minFilterSize == 1 && filterAlign == 2 { filterAlign = 1 }  // NEON
    filterSize = (minFilterSize + (filterAlign - 1)) & ~(filterAlign - 1)
    var f = [Int64](repeating: 0, count: dstW * filterSize)
    for i in 0..<dstW {
        for j in 0..<filterSize { f[i * filterSize + j] = j >= filter2Size ? 0 : filter2[i * filter2Size + j] }
    }
    for i in 0..<dstW {
        if fpos[i] < 0 {
            for j in 1..<max(filterSize, 1) {
                let left = max(j + fpos[i], 0)
                f[i * filterSize + left] += f[i * filterSize + j]
                f[i * filterSize + j] = 0
            }
            fpos[i] = 0
        }
        if fpos[i] + filterSize > srcW {
            let shift = fpos[i] + min(filterSize - srcW, 0)
            var acc: Int64 = 0
            for j in stride(from: filterSize - 1, through: 0, by: -1) where fpos[i] + j >= srcW {
                acc += f[i * filterSize + j]
                f[i * filterSize + j] = 0
            }
            for j in stride(from: filterSize - 1, through: 0, by: -1) {
                f[i * filterSize + j] = j < shift ? 0 : f[i * filterSize + j - shift]
            }
            fpos[i] -= shift
            f[i * filterSize + srcW - 1 - fpos[i]] += acc
        }
        precondition(fpos[i] >= 0 && fpos[i] < srcW)
    }
    var out = [Int16](repeating: 0, count: dstW * filterSize)
    for i in 0..<dstW {
        var error: Int64 = 0, sum: Int64 = 0
        for j in 0..<filterSize { sum += f[i * filterSize + j] }
        sum = (sum + one / 2) / one
        if sum == 0 { sum = 1 }
        for j in 0..<filterSize {
            let v = f[i * filterSize + j] + error
            let intV = roundedDiv(v, sum)
            out[i * filterSize + j] = Int16(truncatingIfNeeded: intV)
            error = v - intV * sum
        }
    }
    return SwsFilter(pos: fpos.map { Int32($0) }, coef: out, size: filterSize)
}

func getLocalPos(_ sub: Int, _ posIn: Int) -> Int {
    var pos = posIn
    if pos == -1 || pos <= -513 { pos = (128 << sub) - 128 }
    pos += 128
    return pos >> sub
}

// ff_yuv2rgb_coeffs rows used: 0/2/5 (BT.601) and 1 (BT.709).
func yuv2rgbCoeffs(_ csp: Int) -> [Int64] {
    csp == 1 ? [117489, 138438, 13975, 34925] : [104597, 132201, 25675, 53279]
}

func roundToInt16(_ f: Int64) -> Int32 {
    let r = Int32(truncatingIfNeeded: (f + (1 << 15)) >> 16)
    if r < -0x7FFF { return Int32(Int16(bitPattern: 0x8000)) }
    if r > 0x7FFF { return 0x7FFF }
    return r
}

// ff_yuv2rgb_c_init_tables, contrast/saturation 1<<16, brightness 0.
struct Yuv2Rgb {
    var yOff: Int32 = 0, yCoef: Int32 = 0, v2r: Int32 = 0, v2g: Int32 = 0, u2g: Int32 = 0, u2b: Int32 = 0
    var ytab = [UInt8](), tabs = [Int32]()
    init(csp: Int, fullRange: Bool) {
        let t = yuv2rgbCoeffs(csp)
        var crv = t[0], cbu = t[1], cgu = -t[2], cgv = -t[3]
        var cy: Int64 = 1 << 16, oy: Int64 = 0
        if !fullRange { cy = (cy * 255) / 219; oy = 16 << 16 } else {
            crv = (crv * 224) / 255; cbu = (cbu * 224) / 255; cgu = (cgu * 224) / 255; cgv = (cgv * 224) / 255
        }
        let contrast: Int64 = 1 << 16, saturation: Int64 = 1 << 16
        cy = (cy * contrast) >> 16
        crv = (crv * contrast * saturation) >> 32
        cbu = (cbu * contrast * saturation) >> 32
        cgu = (cgu * contrast * saturation) >> 32
        cgv = (cgv * contrast * saturation) >> 32
        yCoef = roundToInt16(cy * (1 << 13))
        yOff = roundToInt16(oy * (1 << 9))
        v2r = roundToInt16(crv * (1 << 13))
        v2g = roundToInt16(cgv * (1 << 13))
        u2g = roundToInt16(cgu * (1 << 13))
        u2b = roundToInt16(cbu * (1 << 13))
        crv = ((crv * (1 << 16)) + 0x8000) / max(cy, 1)
        cbu = ((cbu * (1 << 16)) + 0x8000) / max(cy, 1)
        cgu = ((cgu * (1 << 16)) + 0x8000) / max(cy, 1)
        cgv = ((cgv * (1 << 16)) + 0x8000) / max(cy, 1)
        let headroom: Int64 = 512
        let yoffs = (fullRange ? 384 : 326) + 512
        let planeSize = 1024 + 2 * 512
        var yb = -(Int64(384) << 16) - headroom * cy - oy
        ytab = [UInt8](repeating: 0, count: planeSize)
        for i in 0..<planeSize { ytab[i] = UInt8(clamping: (yb + 0x8000) >> 16); yb += cy }
        func off(_ inc: Int64, _ i: Int) -> Int32 { Int32(-(inc >> 9) + ((Int64(i) * inc) >> 16)) }
        tabs = [Int32](repeating: 0, count: 1024)
        for i in 0..<256 {
            tabs[i] = Int32(yoffs) + off(crv, i)
            tabs[256 + i] = Int32(yoffs) + off(cgu, i)
            tabs[512 + i] = off(cgv, i)
            tabs[768 + i] = Int32(yoffs) + off(cbu, i)
        }
    }
}

// fill_rgb2yuv_table for the BT.601 default every RGB->YUV path here resolves to.
let rgb2yuv: (ry: Int32, gy: Int32, by: Int32, ru: Int32, gu: Int32, bu: Int32, rv: Int32, gv: Int32, bv: Int32) = {
    func r(_ v: Double) -> Int32 { Int32(v * Double(1 << 15) + 0.5) }
    return (r(0.299 * 219 / 255), r(0.587 * 219 / 255), r(0.114 * 219 / 255),
            -r(0.169 * 224 / 255), -r(0.331 * 224 / 255), r(0.500 * 224 / 255),
            r(0.500 * 224 / 255), -r(0.419 * 224 / 255), -r(0.081 * 224 / 255))
}()

final class Sws {
    let src: (PF, Int, Int, Meta), dstPF: PF, dw: Int, dh: Int, dmeta: Meta
    enum Kind { case pass, yuv2rgbTab, yuv2rgbNeon, addAlpha, dropAlpha, scaled }
    var kind: Kind = .scaled
    var hl: SwsFilter?, hc: SwsFilter?, vl: SwsFilter?, vc: SwsFilter?
    var chrSrcW = 0, chrSrcH = 0, chrDstW = 0, chrDstH = 0, chrHalf = false
    var y2r: Yuv2Rgb?
    var bufs: [String: MTLBuffer] = [:]
    var rangeLum: (Int32, Int32)?
    var out: Img?
    var tabY: MTLBuffer?, tabT: MTLBuffer?

    init(src: Img, dstPF: PF, w: Int, h: Int, flags: Int) {
        self.src = (src.pf, src.w, src.h, src.meta)
        self.dstPF = dstPF; dw = w; dh = h
        let srgb = src.pf == .rgb || src.pf == .rgba, drgb = dstPF == .rgb || dstPF == .rgba
        var m = Meta()
        m.sar = src.meta.sar
        if drgb { m.range = 1; m.csp = 0 } else if dstPF == .gray { m.range = 1; m.csp = src.meta.csp }
        else { m.range = srgb ? 0 : src.meta.range; m.csp = srgb ? 2 : src.meta.csp; m.hloc = srgb ? 128 : src.meta.hloc; m.vloc = srgb ? 128 : src.meta.vloc }
        if (w, h) != (src.w, src.h) {
            // vf_scale's output SAR keeps the display aspect
            let n = src.meta.sar.0 * h * src.w, d = src.meta.sar.1 * w * src.h
            func gcd(_ a: Int, _ b: Int) -> Int { b == 0 ? a : gcd(b, a % b) }
            let g = gcd(n, d); m.sar = (n / g, d / g)
        }
        dmeta = m
        let unscaled = w == src.w && h == src.h
        let srcRange = srgb ? 0 : src.meta.range, dstRange = drgb ? 0 : m.range
        if unscaled && src.pf == dstPF && srcRange == dstRange { kind = .pass; return }
        if unscaled && (src.pf == .yuv420) && drgb {
            // the aarch64 build dispatches these sizes to NEON, the rest to the C tables
            kind = src.w % 16 == 0 && src.h % 2 == 0 ? .yuv2rgbNeon : .yuv2rgbTab
            y2r = Yuv2Rgb(csp: src.meta.csp, fullRange: src.meta.range == 1)
            tabY = bufOf(y2r!.ytab); tabT = bufOf(y2r!.tabs)
            return
        }
        if unscaled && src.pf == .rgb && dstPF == .rgba { kind = .addAlpha; return }
        if unscaled && src.pf == .rgba && dstPF == .rgb { kind = .dropAlpha; return }
        guard src.pf == .yuv420 || src.pf == .yuva420 || src.pf == .rgb else { die("sws from \(src.pf) not ported") }
        guard dstPF == .yuv420 || dstPF == .gray || dstPF == .rgb else { die("sws to \(dstPF) not ported") }
        kind = .scaled
        let srcW = src.w, srcH = src.h
        var chrSrcHSub = (src.pf == .yuv420 || src.pf == .yuva420) ? 1 : 0
        let chrSrcVSub = chrSrcHSub
        let chrDstHSub = dstPF == .yuv420 ? 1 : 0, chrDstVSub = chrDstHSub
        if srgb && srcW & 1 == 0 && (w >> chrDstHSub) <= (srcW >> 1) { chrSrcHSub = 1; chrHalf = true }
        chrSrcW = (srcW + (1 << chrSrcHSub) - 1) >> chrSrcHSub
        chrSrcH = (srcH + (1 << chrSrcVSub) - 1) >> chrSrcVSub
        chrDstW = (w + (1 << chrDstHSub) - 1) >> chrDstHSub
        chrDstH = (h + (1 << chrDstVSub) - 1) >> chrDstVSub
        let lumXInc = ((Int64(srcW) << 16) + Int64(w >> 1)) / Int64(w)
        let lumYInc = ((Int64(srcH) << 16) + Int64(h >> 1)) / Int64(h)
        let chrXInc = ((Int64(chrSrcW) << 16) + Int64(chrDstW >> 1)) / Int64(chrDstW)
        let chrYInc = ((Int64(chrSrcH) << 16) + Int64(chrDstH >> 1)) / Int64(chrDstH)
        // chroma siting: graph.c strips it for unsubsampled formats (-513)
        let srcH0 = (src.pf == .yuv420 || src.pf == .yuva420) ? src.meta.hloc : -513
        let srcV0 = (src.pf == .yuv420 || src.pf == .yuva420) ? src.meta.vloc : -513
        let dstH0 = dstPF == .yuv420 ? m.hloc : -513, dstV0 = dstPF == .yuv420 ? m.vloc : -513
        hl = initFilter(xInc: lumXInc, srcW: srcW, dstW: w, filterAlign: 4, one: 1 << 14, scaler: flags,
                        srcPos: getLocalPos(0, 0), dstPos: getLocalPos(0, 0))
        vl = initFilter(xInc: lumYInc, srcW: srcH, dstW: h, filterAlign: 2, one: 1 << 12, scaler: flags,
                        srcPos: getLocalPos(0, 0), dstPos: getLocalPos(0, 0))
        if dstPF != .gray {
            hc = initFilter(xInc: chrXInc, srcW: chrSrcW, dstW: chrDstW, filterAlign: 4, one: 1 << 14, scaler: flags,
                            srcPos: getLocalPos(chrSrcHSub, srcH0), dstPos: getLocalPos(chrDstHSub, dstH0))
            vc = initFilter(xInc: chrYInc, srcW: chrSrcH, dstW: chrDstH, filterAlign: 2, one: 1 << 12, scaler: flags,
                            srcPos: getLocalPos(chrSrcVSub, srcV0), dstPos: getLocalPos(chrDstVSub, dstV0))
        }
        if srcRange != dstRange && !drgb {
            // solve_range_convert, 8-bit dst, mpeg -> jpeg (luma; gray needs no chroma)
            let coeff = Int64((((UInt64(255) << 21) / 219) + 127) >> 7)
            let offset = (Int64(255) << 21) - (Int64(235) << 7) * coeff + (1 << 13)
            guard srcRange == 0, dstPF == .gray else { die("range conversion \(srcRange)->\(dstRange) not ported") }
            rangeLum = (Int32(coeff), Int32(offset))
        }
        if drgb { y2r = Yuv2Rgb(csp: srgb ? 0 : src.meta.csp, fullRange: false) }
        for (k, f) in [("hl", hl), ("hc", hc), ("vl", vl), ("vc", vc)] {
            if let f = f { bufs[k + "p"] = bufOf(f.pos); bufs[k + "f"] = bufOf(f.coef) }
        }
        bufs["hY"] = buf(w * srcH * 2)
        if dstPF != .gray { bufs["hU"] = buf(chrDstW * chrSrcH * 2); bufs["hV"] = buf(chrDstW * chrSrcH * 2) }
        if srgb {
            bufs["iY"] = buf(srcW * srcH * 2)
            bufs["iU"] = buf(chrSrcW * chrSrcH * 2); bufs["iV"] = buf(chrSrcW * chrSrcH * 2)
        }
    }

    func run(_ s: Img) -> Img {
        switch kind {
        case .pass: return s
        case .dropAlpha: return Img(.rgb, s.w, s.h, dmeta, planes: Array(s.p[0..<3]))
        case .addAlpha:
            if out == nil { out = Img(.rgba, dw, dh, dmeta, planes: [buf(16), buf(16), buf(16), buf(dw * dh)]); fillPlane(out!.p[3], dw * dh, 255) }
            out!.p[0] = s.p[0]; out!.p[1] = s.p[1]; out!.p[2] = s.p[2]
            return Img(.rgba, dw, dh, dmeta, planes: out!.p)
        default: break
        }
        if out == nil { out = Img(dstPF, dw, dh, dmeta) }
        let o = out!
        if kind == .yuv2rgbNeon {
            let y = y2r!
            gpu.run("k_yuv2rgb_neon", [s.p[0], s.p[1], s.p[2], o.p[0], o.p[1], o.p[2]],
                    [Int32(s.w), Int32(s.h), Int32((s.w + 1) / 2), Int32(Int16(truncatingIfNeeded: y.yOff) >> 6), y.yCoef, y.v2r, y.u2g, y.v2g, y.u2b],
                    grid: (s.w, s.h))
            if dstPF == .rgba { fillPlane(o.p[3], dw * dh, 255) }
            return o
        }
        if kind == .yuv2rgbTab {
            gpu.run("k_yuv2rgb_tab", [s.p[0], s.p[1], s.p[2], o.p[0], o.p[1], o.p[2], tabY!, tabT!],
                    [Int32(s.w), Int32(s.h), Int32((s.w + 1) / 2)], grid: (s.w, s.h))
            if dstPF == .rgba { fillPlane(o.p[3], dw * dh, 255) }
            return o
        }
        let srcW = s.w, srcH = s.h
        let srgb = s.pf == .rgb
        let hl = self.hl!, vl = self.vl!
        // horizontal luma
        if srgb {
            let t = rgb2yuv
            gpu.run("k_rgb2y", [s.p[0], s.p[1], s.p[2], bufs["iY"]!], [Int32(srcW * srcH), t.ry, t.gy, t.by], grid: (srcW * srcH, 1))
            gpu.run("k_hscale16", [bufs["iY"]!, bufs["hY"]!, bufs["hlp"]!, bufs["hlf"]!],
                    [Int32(srcW), Int32(dw), Int32(srcH), Int32(hl.size), 13], grid: (dw, srcH))
        } else {
            gpu.run("k_hscale8", [s.p[0], bufs["hY"]!, bufs["hlp"]!, bufs["hlf"]!],
                    [Int32(srcW), Int32(dw), Int32(srcH), Int32(hl.size)], grid: (dw, srcH))
        }
        if let (c, off) = rangeLum {
            gpu.run("k_lumtojpeg", [bufs["hY"]!], [Int32(dw * srcH), c, off], grid: (dw * srcH, 1))
        }
        if let hc = hc {
            if srgb {
                let t = rgb2yuv
                gpu.run("k_rgb2uv", [s.p[0], s.p[1], s.p[2], bufs["iU"]!, bufs["iV"]!],
                        [Int32(srcW), Int32(srcH), chrHalf ? 1 : 0, t.ru, t.gu, t.bu, t.rv, t.gv, t.bv],
                        grid: (chrSrcW, chrSrcH))
                for (i, k) in [("iU", "hU"), ("iV", "hV")] {
                    gpu.run("k_hscale16", [bufs[i]!, bufs[k]!, bufs["hcp"]!, bufs["hcf"]!],
                            [Int32(chrSrcW), Int32(chrDstW), Int32(chrSrcH), Int32(hc.size), 13], grid: (chrDstW, chrSrcH))
                }
            } else {
                for (i, k) in [(1, "hU"), (2, "hV")] {
                    gpu.run("k_hscale8", [s.p[i], bufs[k]!, bufs["hcp"]!, bufs["hcf"]!],
                            [Int32(chrSrcW), Int32(chrDstW), Int32(chrSrcH), Int32(hc.size)], grid: (chrDstW, chrSrcH))
                }
            }
        }
        // vertical
        switch dstPF {
        case .yuv420, .gray:
            gpu.run("k_vplane", [bufs["hY"]!, o.p[0], bufs["vlp"]!, bufs["vlf"]!], [Int32(dw), Int32(dh), Int32(vl.size)], grid: (dw, dh))
            if dstPF == .yuv420, let vc = vc {
                for (k, pi) in [("hU", 1), ("hV", 2)] {
                    gpu.run("k_vplane", [bufs[k]!, o.p[pi], bufs["vcp"]!, bufs["vcf"]!],
                            [Int32(chrDstW), Int32(chrDstH), Int32(vc.size)], grid: (chrDstW, chrDstH))
                }
            }
        case .rgb:
            let y = y2r!, vc = self.vc!
            gpu.run("k_vgbrp", [bufs["hY"]!, bufs["hU"]!, bufs["hV"]!, o.p[0], o.p[1], o.p[2],
                                bufs["vlp"]!, bufs["vlf"]!, bufs["vcp"]!, bufs["vcf"]!],
                    [Int32(dw), Int32(dh), Int32(vl.size), Int32(vc.size), y.yOff, y.yCoef, y.v2r, y.v2g, y.u2g, y.u2b],
                    grid: (dw, dh))
        default: die("unreachable")
        }
        return o
    }
}

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------
struct InSpec { var path = "", t: Int64? = nil, framerate: Int? = nil, loop = false }

func parseInputs(_ argv: [String]) -> [InSpec] {
    var out: [InSpec] = [], cur = InSpec(), i = 0
    while i < argv.count {
        let a = argv[i], v = i + 1 < argv.count ? argv[i + 1] : ""
        switch a {
        case "-i": cur.path = v; out.append(cur); cur = InSpec()
        case "-ss": if durationUs(v) != 0 { die("-ss \(v) not ported") }
        case "-t": cur.t = durationUs(v)
        case "-framerate": cur.framerate = Int(v)
        case "-stream_loop": cur.loop = v == "-1"
        case "-thread_queue_size": break
        default: die("input option \(a) not ported")
        }
        i += 2
    }
    return out
}

final class VideoIn {
    let spec: InSpec
    var asset: AVURLAsset!, track: AVAssetTrack!
    var reader: AVAssetReader!, output: AVAssetReaderTrackOutput!
    var w = 0, h = 0, meta = Meta()
    var emptyEdit = CMTime.zero
    var next = 0            // index of the next frame copyNext will deliver
    var slots: [Img] = []
    var cache: [Int: Img] = [:]

    init(_ s: InSpec) {
        spec = s
        asset = AVURLAsset(url: URL(fileURLWithPath: s.path))
        guard let t = asset.tracks(withMediaType: .video).first else { die("no video in \(s.path)") }
        track = t
        let sz = t.naturalSize
        w = Int(sz.width); h = Int(sz.height)
        let fd = t.formatDescriptions[0] as! CMFormatDescription
        let ext = CMFormatDescriptionGetExtensions(fd) as? [String: Any] ?? [:]
        meta.range = (ext["FullRangeVideo"] as? Bool ?? false) ? 1 : 0
        switch ext["CVImageBufferYCbCrMatrix"] as? String {
        case nil: meta.csp = 2
        case "ITU_R_709_2"?: meta.csp = 1
        case let m?: die("YCbCr matrix \(m) not ported")
        }
        // the chroma location tag is not read: ffmpeg's scale resolves to
        // centre siting on these left-tagged streams (measured: its output is
        // byte-identical to in_h_chr_pos=128:out_h_chr_pos=128)
        if let seg = t.segments.first, seg.isEmpty { emptyEdit = seg.timeMapping.target.duration }
        slots = (0..<3).map { _ in Img(.yuv420, w, h, meta) }
        open()
    }
    func open() {
        reader = try! AVAssetReader(asset: asset)
        output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange])
        output.alwaysCopiesSampleData = false
        reader.add(output)
        reader.startReading()
    }
    // Next real frame's pixel buffer and pts (seconds since the first real frame), or nil at EOF.
    func readNext() -> (CVPixelBuffer, CMTime)? {
        while let sb = output.copyNextSampleBuffer() {
            guard let pb = CMSampleBufferGetImageBuffer(sb) else { continue }
            let t = CMSampleBufferGetPresentationTimeStamp(sb)
            // the frame AVFoundation presents for an empty edit is not a frame ffmpeg decodes
            if CMTimeCompare(t, emptyEdit) < 0 { continue }
            return (pb, CMTimeSubtract(t, emptyEdit))
        }
        return nil
    }
    func upload(_ pb: CVPixelBuffer, into img: Img) {
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
        guard CVPixelBufferGetWidth(pb) == w, CVPixelBufferGetHeight(pb) == h else { die("frame size changed in \(spec.path)") }
        let yb = CVPixelBufferGetBaseAddressOfPlane(pb, 0)!.assumingMemoryBound(to: UInt8.self)
        let ys = CVPixelBufferGetBytesPerRowOfPlane(pb, 0)
        let Y = img.p[0].contents().assumingMemoryBound(to: UInt8.self)
        for r in 0..<h { memcpy(Y + r * w, yb + r * ys, w) }
        let uvb = CVPixelBufferGetBaseAddressOfPlane(pb, 1)!.assumingMemoryBound(to: UInt8.self)
        let uvs = CVPixelBufferGetBytesPerRowOfPlane(pb, 1)
        let cw = (w + 1) / 2, ch = (h + 1) / 2
        let U = img.p[1].contents().assumingMemoryBound(to: UInt8.self), V = img.p[2].contents().assumingMemoryBound(to: UInt8.self)
        for r in 0..<ch {
            let row = uvb + r * uvs
            for x in 0..<cw { U[r * cw + x] = row[2 * x]; V[r * cw + x] = row[2 * x + 1] }
        }
    }
    // Frame k of the (-stream_loop'd) stream, decoded strictly in order.
    func frame(_ k: Int) -> Img {
        if let c = cache[k] { return c }
        guard k >= next else { die("\(spec.path): frame \(k) requested after \(next)") }
        while next <= k {
            var r = readNext()
            if r == nil && spec.loop && next > 0 { open(); r = readNext() }
            guard let (pb, _) = r else { die("\(spec.path) has no frame \(k)") }
            let img = slots[next % slots.count]
            upload(pb, into: img)
            cache = [next: img]
            next += 1
        }
        return cache[k]!
    }
}

// The source's presentation timeline (from the compressed samples, no
// decode) and its frame duration, for fps= mapping. -t keeps pts < t.
func probeTimeline(_ v: VideoIn) -> ([CMTime], CMTime) {
    let r = try! AVAssetReader(asset: v.asset)
    let o = AVAssetReaderTrackOutput(track: v.track, outputSettings: nil)
    r.add(o); r.startReading()
    var ts: [CMTime] = [], fdur = CMTime.zero
    let edits = v.track.segments.filter { !$0.isEmpty }
    guard edits.count == 1, CMTimeCompare(edits[0].timeMapping.source.duration, edits[0].timeMapping.target.duration) == 0
    else { die("\(v.spec.path): edit list not ported") }
    let edit = edits[0].timeMapping
    while let sb = o.copyNextSampleBuffer() {
        if CMSampleBufferGetNumSamples(sb) == 0 { continue }
        let t = CMSampleBufferGetPresentationTimeStamp(sb)
        // undecoded samples carry media time; the edit list maps it to the
        // presentation time ffmpeg's mov demuxer gives the frame
        if CMTimeCompare(t, edit.source.start) < 0 { continue }
        ts.append(CMTimeAdd(CMTimeSubtract(t, edit.source.start), edit.target.start))
        fdur = CMSampleBufferGetDuration(sb)
    }
    ts.sort { CMTimeCompare($0, $1) < 0 }
    if let lim = v.spec.t {
        ts = ts.filter { CMTimeCompare(CMTimeConvertScale($0, timescale: 1_000_000, method: .roundTowardPositiveInfinity),
                                       CMTime(value: lim, timescale: 1_000_000)) < 0 }
    }
    return (ts, fdur)
}

func loadPNG(_ path: String) -> Img {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let im = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCache: false] as CFDictionary) else { die("cannot read \(path)") }
    let w = im.width, h = im.height, bpp = im.bitsPerPixel, row = im.bytesPerRow
    guard im.bitsPerComponent == 8 else { die("\(path): not 8-bit") }
    let ai = im.alphaInfo
    let hasA: Bool
    switch ai {
    case .last: hasA = true
    case .none: hasA = false
    case .noneSkipLast: hasA = false
    default: die("\(path): alpha layout \(ai.rawValue) not ported")
    }
    let data = im.dataProvider!.data! as Data
    var m = Meta(); m.range = 1; m.csp = 0
    let img = Img(hasA ? .rgba : .rgb, w, h, m)
    let ptrs = img.p.map { $0.contents().assumingMemoryBound(to: UInt8.self) }
    let step = bpp / 8
    data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        let b = raw.bindMemory(to: UInt8.self)
        for y in 0..<h {
            for x in 0..<w {
                let o = y * row + x * step
                for c in 0..<(hasA ? 4 : 3) { ptrs[c][y * w + x] = b[o + c] }
            }
        }
    }
    return img
}

// ---------------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------------
class Node {
    var inputs: [Node] = []
    var constant = false
    var lastN = Int.min
    var last: Img?
    var label = ""
    var tb: Int64 = 0       // time base denominator (numerator 1), as ffmpeg's link carries it
    // frame N of this node's own stream
    func get(_ n: Int) -> Img {
        if constant, let l = last { return l }
        if n == lastN, let l = last { return l }
        let r = compute(n)
        lastN = n; last = r
        return r
    }
    func compute(_ n: Int) -> Img { die("compute not implemented") }
}

final class InputNode: Node {
    let v: VideoIn?
    let png: Img?
    init(_ s: InSpec) {
        if s.path.lowercased().hasSuffix(".png") { png = loadPNG(s.path); v = nil } else { v = VideoIn(s); png = nil }
        super.init()
        constant = png != nil
        tb = v.map { Int64($0.track.naturalTimeScale) } ?? Int64(s.framerate ?? 25)
    }
    override func compute(_ n: Int) -> Img { png ?? v!.frame(n) }
}

final class FuncNode: Node {
    let f: (Int, [Img]) -> Img
    let sequential: Bool
    var done = -1
    // `sequential`: the filter carries state from frame to frame, so it runs
    // on every frame of its stream in order, as ffmpeg's would, even on the
    // ones a framesync downstream then discards.
    init(_ ins: [Node], constant c: Bool = false, sequential: Bool = false, _ f: @escaping (Int, [Img]) -> Img) {
        self.f = f
        self.sequential = sequential
        super.init()
        inputs = ins
        constant = c && ins.allSatisfy { $0.constant }
    }
    override func compute(_ n: Int) -> Img {
        if sequential { while done < n - 1 { done += 1; _ = f(done, inputs.map { $0.get(done) }) }; done = n }
        return f(n, inputs.map { $0.get(n) })
    }
}

final class FpsNode: Node {
    var map: [Int] = []   // output pts - first -> source frame
    var first: Int64 = 0
    var visited = -1
    init(_ src: Node, timeline: ([CMTime], CMTime), rate: Int64) {
        super.init()
        inputs = [src]
        tb = rate
        // vf_fps, rounding=near: output N shows the last input whose pts
        // rounds to <= N; EOF closes the stream at the rounded pts one frame
        // duration past the last input.
        func r(_ t: CMTime) -> Int64 { rescale(t.value, 1, Int64(t.timescale), 1, rate) }
        let op = timeline.0.map(r)
        guard let f0 = op.first else { die("source has no frames") }
        first = f0
        let end = r(CMTimeAdd(timeline.0.last!, timeline.1))
        var k = 0, n = f0
        while n < end {
            while k + 1 < op.count && op[k + 1] <= n { k += 1 }
            map.append(k)
            n += 1
        }
    }
    // every source frame reaches the filters before fps, in order, even one
    // fps drops: vignette's dither runs on each
    override func compute(_ n: Int) -> Img {
        let k = map[min(max(n - Int(first), 0), map.count - 1)]
        while visited < k - 1 { visited += 1; _ = inputs[0].get(visited) }
        visited = k
        return inputs[0].get(k)
    }
}

func lut256(_ f: (Int) -> Int) -> MTLBuffer { bufOf((0..<256).map { UInt8(clamping: f($0)) }) }

final class Graph {
    var labels: [String: Node] = [:]
    var inputs: [InputNode] = []
    var inSpecs: [InSpec] = []
    let chains: [Chain]
    var consumers: [String: [(Int, Int)]] = [:]
    var srcTimeline: ([CMTime], CMTime) = ([], .zero)

    init(fc: String, specs: [InSpec]) {
        chains = parseGraph(fc).filter { !$0.ins.contains("0:a") }
        inSpecs = specs
        for (ci, c) in chains.enumerated() { for l in c.ins { consumers[l, default: []].append((ci, 0)) } }
        inputs = specs.map { InputNode($0) }
        for (i, n) in inputs.enumerated() { labels["\(i):v"] = n }
        if let v = inputs.first?.v { srcTimeline = probeTimeline(v) }
        var pending = Array(chains.indices)
        while !pending.isEmpty {
            let before = pending.count
            pending = pending.filter { ci in
                let c = chains[ci]
                guard c.ins.allSatisfy({ labels[$0] != nil }) else { return true }
                build(ci)
                return false
            }
            if pending.count == before { die("graph has unresolved labels: \(pending.map { chains[$0].ins })") }
        }
    }

    // The format a later `format=` in this chain (or past single-consumer
    // labels, through format-agnostic filters) asks for: what scale negotiates.
    func downstreamFormat(_ ci: Int, after fi: Int) -> String? {
        var c = ci, f = fi + 1
        for _ in 0..<64 {
            let ch = chains[c]
            while f < ch.filters.count {
                let s = ch.filters[f]
                if s.name == "format" { return s.pos.first ?? s.kv["pix_fmts"] }
                if !["fade", "pad", "setsar"].contains(s.name) { return nil }
                f += 1
            }
            guard ch.outs.count == 1, let cons = consumers[ch.outs[0]], cons.count == 1 else { return nil }
            c = cons[0].0; f = 0
            if chains[c].ins.count != 1 { return nil }
        }
        return nil
    }

    func build(_ ci: Int) {
        let c = chains[ci]
        var cur: [Node] = c.ins.map { labels[$0]! }
        for (fi, s) in c.filters.enumerated() {
            let ins = cur
            cur = make(s, ins, ci, fi)
            // framesync filters run on the gcd of their inputs' time bases; overlay keeps the main's
            let fsync = ["blend", "displace", "alphamerge"].contains(s.name)
            for n in cur where n.tb == 0 {
                func gcd(_ a: Int64, _ b: Int64) -> Int64 { b == 0 ? a : gcd(b, a % b) }
                n.tb = fsync ? ins.map { $0.tb }.reduce(1) { $0 / gcd($0, $1) * $1 } : ins[0].tb
            }
        }
        if cur.count != c.outs.count { die("chain output count mismatch at \(c.outs)") }
        for (o, n) in zip(c.outs, cur) { labels[o] = n; n.label = o }
    }

    func pfOf(_ name: String) -> PF {
        switch name {
        case "gbrp", "rgb24": return .rgb
        case "rgba", "argb", "gbrap": return .rgba
        case "yuv420p": return .yuv420
        case "yuva420p": return .yuva420
        case "gray": return .gray
        default: die("pixel format \(name) not ported")
        }
    }

    // `n` in one of `pfs`, else converted to `prefer` as ffmpeg's
    // auto-inserted scale (default flags: bicubic) does it.
    func need(_ n: Node, _ pfs: [PF], prefer: PF) -> Node {
        var sws: Sws?
        return FuncNode([n], constant: true) { _, ins in
            if pfs.contains(ins[0].pf) { return ins[0] }
            if sws == nil { sws = Sws(src: ins[0], dstPF: prefer, w: ins[0].w, h: ins[0].h, flags: SWS_BICUBIC) }
            return sws!.run(ins[0])
        }
    }

    func make(_ s: FSpec, _ ins: [Node], _ ci: Int, _ fi: Int) -> [Node] {
        if s.enable != nil && s.name != "overlay" { die("enable= on \(s.name) not ported") }
        switch s.name {
        case "split":
            let n = Int(s.pos.first ?? "2")!
            return Array(repeating: ins[0], count: n)
        case "setsar":
            return [FuncNode(ins, constant: true) { _, i in
                let o = Img(i[0].pf, i[0].w, i[0].h, i[0].meta, planes: i[0].p); o.meta.sar = (1, 1); return o }]
        case "format":
            let pf = pfOf(s.pos.first ?? s.kv["pix_fmts"]!)
            return [need(ins[0], [pf], prefer: pf)]
        case "loop":
            guard ins[0].constant else { die("loop over a non-constant stream not ported") }
            return ins
        case "fps":
            guard Int64(s.pos.first ?? "") == 30 else { die("fps \(s.pos) not ported") }
            return [FpsNode(ins[0], timeline: srcTimeline, rate: 30)]
        case "crop": return [crop(s, ins[0])]
        case "scale": return [scale(s, ins[0], ci, fi)]
        case "eq": return [eq(s, ins[0])]
        case "colorbalance": return [colorbalance(s, ins[0])]
        case "vignette": return [vignette(s, ins[0])]
        case "blend": return [blend(s, ins)]
        case "color": return [color(s)]
        case "overlay": return [overlay(s, ins)]
        case "gblur": return [gblur(s, ins[0])]
        case "alphaextract":
            return [FuncNode([need(ins[0], [.rgba, .yuva420], prefer: .rgba)], constant: true) { _, i in
                Img(.gray, i[0].w, i[0].h, Meta(range: 1, csp: 2), planes: [i[0].p[3]]) }]
        case "alphamerge":
            return [FuncNode([need(ins[0], [.rgba], prefer: .rgba), need(ins[1], [.gray], prefer: .gray)], constant: true) { _, i in
                guard i[0].w == i[1].w && i[0].h == i[1].h else { die("alphamerge size mismatch") }
                return Img(.rgba, i[0].w, i[0].h, i[0].meta, planes: [i[0].p[0], i[0].p[1], i[0].p[2], i[1].p[0]]) }]
        case "fade": return [fade(s, ins[0])]
        case "colorchannelmixer": return [mixer(s, ins[0])]
        case "lutrgb": return [lutrgb(s, ins[0])]
        case "scroll": return [scroll(s, ins[0])]
        case "displace": return [displace(s, ins)]
        case "pad": return [pad(s, ins[0])]
        default: die("filter \(s.name) not ported")
        }
    }

    func crop(_ s: FSpec, _ n: Node) -> Node {
        var out: Img?
        return FuncNode([n], constant: true) { _, i in
            let src = i[0]
            let vars: [String: Double] = ["iw": Double(src.w), "ih": Double(src.h), "in_w": Double(src.w), "in_h": Double(src.h)]
            let w = Int(Expr.parse(s.opt("w", 0) ?? "iw").eval(vars)), h = Int(Expr.parse(s.opt("h", 1) ?? "ih").eval(vars))
            var x = Int(Expr.parse(s.opt("x", 2) ?? "\((src.w - w) / 2)").eval(vars))
            var y = Int(Expr.parse(s.opt("y", 3) ?? "\((src.h - h) / 2)").eval(vars))
            if (w, h, x, y) == (src.w, src.h, 0, 0) { return src }
            if src.pf == .yuv420 || src.pf == .yuva420 { x &= ~1; y &= ~1 }
            if out == nil { out = Img(src.pf, w, h, src.meta) }
            for pi in 0..<src.p.count {
                let sub = (src.pf == .yuv420 || src.pf == .yuva420) && (pi == 1 || pi == 2) ? 1 : 0
                let (pw, ph) = out!.dims(pi), (sw, _) = src.dims(pi)
                copyPlane(src.p[pi], out!.p[pi], w: pw, h: ph, sstride: sw, sx: x >> sub, sy: y >> sub, dstride: pw)
            }
            return out!
        }
    }

    func scale(_ s: FSpec, _ n: Node, _ ci: Int, _ fi: Int) -> Node {
        let flags = swsFlag(s.kv["flags"] ?? "bicubic")
        let want = downstreamFormat(ci, after: fi).map { pfOf($0) }
        var sws: Sws?
        return FuncNode([n], constant: true) { _, i in
            let src = i[0]
            let w = Int(num(s.opt("w", 0)!)), h = Int(num(s.opt("h", 1)!))
            if sws == nil { sws = Sws(src: src, dstPF: want ?? src.pf, w: w, h: h, flags: flags) }
            return sws!.run(src)
        }
    }

    func eq(_ s: FSpec, _ n: Node) -> Node {
        // vf_eq: options are expressions, stored via av_clipf (float)
        func p(_ k: String, _ d: String, _ lo: Double, _ hi: Double) -> Double {
            Double(Float(min(max(num(s.kv[k] ?? d), lo), hi)))
        }
        let contrast = p("contrast", "1", -1000, 1000), brightness = p("brightness", "0", -1, 1)
        let saturation = p("saturation", "1", 0, 3), gamma = p("gamma", "1", 0.1, 10), gw = p("gamma_weight", "1", 0, 1)
        for k in s.kv.keys where !["contrast", "brightness", "saturation", "gamma"].contains(k) { die("eq \(k) not ported") }
        func lutFor(contrast c: Double, brightness b: Double, gamma g0: Double) -> [UInt8] {
            if c == 1.0 && b == 0.0 && g0 == 1.0 { return (0..<256).map { UInt8($0) } }
            if g0 == 1.0 && abs(c) < 7.9 {
                // process_c
                let ci = Int(c * 256 * 16)
                let bi = (Int(100.0 * b + 100.0) * 511) / 200 - 128 - ci / 32
                return (0..<256).map { v in
                    var pel = ((v * ci) >> 12) + bi
                    if pel & ~255 != 0 { pel = (-pel) >> 31 & 255 }
                    return UInt8(pel & 255)
                }
            }
            // create_lut: `contrast * (v - 0.5) + 0.5` and `v*lw + pow*gw` contract to fma
            let g = 1.0 / g0, lw = 1.0 - gw
            return (0..<256).map { i in
                var v = fma(c, Double(i) / 255.0 - 0.5, 0.5) + b
                if v <= 0.0 { return 0 }
                v = fma(v, lw, pow(v, g) * gw)
                if v >= 1.0 { return 255 }
                return UInt8(256.0 * v)
            }
        }
        let gammaG = 1.0
        let ly = bufOf(lutFor(contrast: contrast, brightness: brightness, gamma: gamma * gammaG))
        let lc = bufOf(lutFor(contrast: saturation, brightness: 0, gamma: (1.0 / gammaG).squareRoot()))
        var out: Img?
        return FuncNode([n]) { _, i in
            let src = i[0]
            guard src.pf == .yuv420 else { die("eq on \(src.pf) not ported") }
            if out == nil { out = Img(.yuv420, src.w, src.h, src.meta) }
            out!.meta = src.meta
            for pi in 0..<3 {
                let (pw, ph) = src.dims(pi)
                gpu.run("k_lut", [src.p[pi], out!.p[pi], pi == 0 ? ly : lc], [Int32(pw * ph)], grid: ((pw * ph + 3) / 4, 1))
            }
            return out!
        }
    }

    func colorbalance(_ s: FSpec, _ n0: Node) -> Node {
        func f(_ k: String) -> Float { Float(num(s.kv[k] ?? "0")) }
        for k in s.kv.keys where !["rs", "gs", "bs", "rm", "gm", "bm", "rh", "gh", "bh"].contains(k) { die("colorbalance \(k) not ported") }
        let sh = [f("rs"), f("gs"), f("bs")], md = [f("rm"), f("gm"), f("bm")], hi = [f("rh"), f("gh"), f("bh")]
        let vl = (0..<256).map { Float($0) / Float(255) }
        // get_component's three terms per channel for every (max, min) byte pair;
        // `(x) * a + 0.5f` contracts to fmaf under clang's fp-contract=on
        var smh = [Float](repeating: 0, count: 65536 * 9)
        let a: Float = 4, b: Float = 0.333, scale: Float = 0.7
        func clipf(_ v: Float) -> Float { min(max(v, 0), 1) }
        for mx in 0..<256 {
            for mn in 0...mx {
                let l = vl[mx] + vl[mn]
                let c1 = clipf(fmaf(b - l, a, 0.5))
                let c2 = clipf(fmaf(l - b, a, 0.5)), c3 = clipf(fmaf(1 - l - b, a, 0.5))
                let c4 = clipf(fmaf(l + b - 1, a, 0.5))
                for ch in 0..<3 {
                    let k = (mx * 256 + mn) * 9 + ch * 3
                    smh[k] = sh[ch] * (c1 * scale)
                    smh[k + 1] = md[ch] * (c2 * c3 * scale)
                    smh[k + 2] = hi[ch] * (c4 * scale)
                }
            }
        }
        let bvl = bufOf(vl), bsmh = bufOf(smh)
        let n = need(n0, [.rgb], prefer: .rgb)
        var out: Img?
        return FuncNode([n]) { _, i in
            let src = i[0]
            if out == nil { out = Img(.rgb, src.w, src.h, src.meta) }
            out!.meta = src.meta
            gpu.run("k_cbal", [src.p[0], src.p[1], src.p[2], out!.p[0], out!.p[1], out!.p[2], bvl, bsmh],
                    [Int32(src.w * src.h)], grid: (src.w * src.h, 1))
            return out!
        }
    }

    func vignette(_ s: FSpec, _ n0: Node) -> Node {
        for k in s.kv.keys where k != "a" && k != "angle" { die("vignette \(k) not ported") }
        let angle = Double(Float(min(max(num(s.kv["a"] ?? s.kv["angle"] ?? "PI/5"), 0), Double.pi / 2)))
        let n = need(n0, [.rgb], prefer: .rgb)
        var out: Img?, fmap: MTLBuffer?, A: MTLBuffer?, C: MTLBuffer?
        var stepA: UInt32 = 1, stepC: UInt32 = 0, state: UInt32 = 0, frames = 0
        return FuncNode([n]) { k, i in
            let src = i[0]
            let W = src.w, H = src.h
            if out == nil {
                out = Img(.rgb, W, H, src.meta)
                var sar = src.meta.sar
                if sar.0 == 0 || sar.1 == 0 { sar = (1, 1) }
                var xscale: Float = 1, yscale: Float = 1
                if sar.0 > sar.1 { xscale = Float(Double(sar.0) / Double(sar.1)) } else { yscale = Float(Double(sar.1) / Double(sar.0)) }
                let x0 = Double(W) / 2, y0 = Double(H) / 2
                let dmax = hypot(Double(W) / 2.0, Double(H) / 2.0)
                var fm = [Float](repeating: 0, count: W * H)
                for y in 0..<H {
                    for x in 0..<W {
                        let xx = Int((Double(x) - x0) * Double(xscale)), yy = Int((Double(y) - y0) * Double(yscale))
                        let dnorm = hypot(Double(xx), Double(yy)) / dmax
                        if dnorm > 1 { fm[y * W + x] = 0 } else { let c = cos(angle * dnorm); fm[y * W + x] = Float((c * c) * (c * c)) }
                    }
                }
                fmap = bufOf(fm)
                // LCG d = d*1664525 + 1013904223 jumped ahead: state(base + p) = A[p]*state(base) + C[p]
                let n = W * H * 3
                var a = [UInt32](repeating: 0, count: n), c = [UInt32](repeating: 0, count: n)
                var ap: UInt32 = 1, cp: UInt32 = 0
                for p in 0..<n { a[p] = ap; c[p] = cp; ap = ap &* 1664525; cp = cp &* 1664525 &+ 1013904223 }
                stepA = ap; stepC = cp
                A = bufOf(a); C = bufOf(c)
            }
            // frames reach vignette in source order, each exactly once
            precondition(k == frames, "vignette frames out of order")
            gpu.run("k_vign", [src.p[0], src.p[1], src.p[2], out!.p[0], out!.p[1], out!.p[2], fmap!, A!, C!],
                    [Int32(W * H), Int32(bitPattern: state)], grid: (W * H, 1))
            state = stepA &* state &+ stepC
            frames += 1
            out!.meta = src.meta
            return out!
        }
    }

    func blend(_ s: FSpec, _ ins: [Node]) -> Node {
        let mode: Int32
        switch s.kv["all_mode"] { case "multiply"?: mode = 0; case "screen"?: mode = 1; case "addition"?: mode = 2
        default: die("blend \(s.kv) not ported") }
        var out: Img?
        return FuncNode(ins, constant: true) { _, i in
            let a = i[0], b = i[1]
            guard a.pf == b.pf || (a.pf == .gray && b.pf == .gray), a.w == b.w, a.h == b.h else { die("blend input mismatch \(a.pf) \(b.pf)") }
            if out == nil { out = Img(a.pf, a.w, a.h, a.meta) }
            for pi in 0..<a.p.count {
                let (pw, ph) = a.dims(pi)
                gpu.run("k_blend", [a.p[pi], b.p[pi], out!.p[pi]], [Int32(pw * ph), mode], grid: ((pw * ph + 3) / 4, 1))
            }
            return out!
        }
    }

    // color= sources: their format is whatever the consumer negotiates; the
    // graph only feeds them to overlay (yuva420p for the wipe plates, an
    // RGB+alpha format for the glow plates) or alphamerge.
    func color(_ s: FSpec) -> Node {
        let c = s.kv["c"] ?? s.kv["color"] ?? "black"
        let size = (s.kv["s"] ?? s.kv["size"]!).split(separator: "x").map { Int($0)! }
        guard c == "black" || c == "white" else { die("color \(c) not ported") }
        let white = c == "white"
        let node = ColorNode(w: size[0], h: size[1], white: white)
        return node
    }

    func overlay(_ s: FSpec, _ ins: [Node]) -> Node {
        guard s.kv["format"] == "auto" || s.kv["format"] == nil else { die("overlay format \(s.kv["format"]!) not ported") }
        let xe = Expr.parse(s.opt("x", 0) ?? "0"), ye = Expr.parse(s.opt("y", 1) ?? "0")
        let en = s.enable.map { Expr.parse($0) }
        var out: Img?
        let tb = ins[0].tb
        guard tb % 30 == 0 else { die("overlay on time base 1/\(tb) not ported") }
        // the overlay input arrives in the main input's format family
        return FuncNode(ins) { n, i in
            var main = i[0], ov = i[1]
            let t = Double(Int64(n) * (tb / 30)) * (1.0 / Double(tb))
            // a plate under a plate negotiates the first of overlay's alpha
            // formats, yuva420p; a plate under a layer takes the layer's
            if let a = ins[0] as? ColorNode, let b = ins[1] as? ColorNode { main = a.as(.yuva420); ov = b.as(.yuva420) }
            else if let a = ins[0] as? ColorNode { main = a.as(ov.pf) }
            if let en = en, en.eval(["t": t, "n": Double(n)]) == 0 { return main }
            if ov.pf != main.pf { die("overlay formats \(main.pf) over \(ov.pf) not ported") }
            let vars: [String: Double] = ["t": t, "n": Double(n), "W": Double(main.w), "H": Double(main.h),
                                          "w": Double(ov.w), "h": Double(ov.h), "main_w": Double(main.w), "main_h": Double(main.h),
                                          "overlay_w": Double(ov.w), "overlay_h": Double(ov.h)]
            let sub = main.pf == .yuva420 ? 1 : 0
            func norm(_ d: Double) -> Int { d.isNaN ? Int(Int32.max) : Int(Int32(truncatingIfNeeded: Int(d))) & ~((1 << sub) - 1) }
            let x = norm(xe.eval(vars)), y = norm(ye.eval(vars))
            if out == nil { out = Img(main.pf, main.w, main.h, main.meta) }
            let o = out!
            copyImg(main, o)
            guard x < main.w && x + ov.w >= 0 && y < main.h && y + ov.h >= 0 else { return o }
            let ip = [main.w, main.h, ov.w, ov.h, x, y].map { Int32($0) }
            if main.pf == .rgba {
                gpu.run("k_ovl_rgba", [o.p[0], o.p[1], o.p[2], o.p[3], ov.p[0], ov.p[1], ov.p[2], ov.p[3], main.p[3]], ip, grid: (ov.w, ov.h))
            } else if main.pf == .yuva420 {
                for pi in 0..<3 {
                    let s2 = pi == 0 ? 0 : 1
                    let (dwp, dhp) = main.dims(pi), (swp, shp) = ov.dims(pi)
                    gpu.run("k_ovl_yuva_plane", [o.p[pi], ov.p[pi], ov.p[3], main.p[3]],
                            [dwp, dhp, swp, shp, x >> s2, y >> s2, s2, ov.w, main.w].map { Int32($0) }, grid: (swp, shp))
                }
                gpu.run("k_alpha_comp", [o.p[3], ov.p[3], main.p[3]], ip, grid: (ov.w, ov.h))
            } else { die("overlay on \(main.pf) not ported") }
            return o
        }
    }

    func gblur(_ s: FSpec, _ n: Node) -> Node {
        for k in s.kv.keys where k != "sigma" && k != "steps" { die("gblur \(k) not ported") }
        let sigma = Float(num(s.kv["sigma"] ?? "0.5")), steps = Int(s.kv["steps"] ?? "1")!
        // set_params, in double from the float sigma
        let lambda = Double(sigma * sigma) / (2.0 * Double(steps))
        let dnu = (1.0 + 2.0 * lambda - (1.0 + 4.0 * lambda).squareRoot()) / (2.0 * lambda)
        var post = Float(pow(dnu / lambda, Double(steps)))
        var bscale = Float(1.0 / (1.0 - dnu))
        var nu = Float(dnu)
        if !post.isNormal { post = 1 }
        if !bscale.isNormal { bscale = 1 }
        if !nu.isNormal { nu = 0 }
        let postscale = post * post
        var out: Img?, fb: [MTLBuffer]?
        return FuncNode([n], constant: true) { _, i in
            let src = i[0]
            let np = src.p.count, dims = (0..<np).map { src.dims($0) }
            if out == nil {
                out = Img(src.pf, src.w, src.h, src.meta)
                fb = dims.flatMap { [buf($0.0 * $0.1 * 4), buf($0.0 * $0.1 * 4)] }
            }
            for pi in 0..<np { gpu.run("k_g_load", [src.p[pi], fb![2 * pi]], [Int32(dims[pi].0), Int32(dims[pi].1)], grid: dims[pi]) }
            for pi in 0..<np { gpu.run("k_g_h", [fb![2 * pi]], [Int32(dims[pi].0), Int32(dims[pi].1), Int32(steps)], fp: [nu, bscale], grid: (dims[pi].1, 1), tg: 32) }
            for pi in 0..<np { gpu.run("k_g_tr", [fb![2 * pi], fb![2 * pi + 1]], [Int32(dims[pi].0), Int32(dims[pi].1)], grid: dims[pi]) }
            for pi in 0..<np { gpu.run("k_g_v", [fb![2 * pi + 1]], [Int32(dims[pi].0), Int32(dims[pi].1), Int32(steps)], fp: [nu, bscale], grid: (dims[pi].0, 1), tg: 32) }
            for pi in 0..<np { gpu.run("k_g_store", [fb![2 * pi + 1], out!.p[pi]], [Int32(dims[pi].0 * dims[pi].1)], fp: [postscale], grid: (dims[pi].0 * dims[pi].1, 1)) }
            return out!
        }
    }

    func fade(_ s: FSpec, _ n: Node) -> Node {
        for k in s.kv.keys where !["t", "st", "d", "alpha"].contains(k) { die("fade \(k) not ported") }
        let out_ = s.kv["t"] == "out"
        let alpha = s.kv["alpha"] == "1"
        var outImg: Img?
        let tb = n.tb
        guard tb % 30 == 0 else { die("fade on time base 1/\(tb) not ported") }
        let st = rescale(durationUs(s.kv["st"] ?? "0"), 1, 1_000_000, 1, tb)
        let d = rescale(durationUs(s.kv["d"] ?? "0"), 1, 1_000_000, 1, tb)
        guard d > 0 else { die("frame-count fades not ported") }
        return FuncNode([n]) { k, i in
            let src = i[0]
            let pts = Int64(k) * (tb / 30)
            // vf_fade's state machine collapses to this for monotonic pts
            var f: Int64 = pts < st ? 0 : (pts > st + d ? 65535 : (pts - st) * 65535 / d)
            f = min(max(f, 0), 65535)
            if out_ { f = 65535 - f }
            if f >= 65535 { return src }
            if alpha {
                guard src.pf == .rgba else { die("alpha fade on \(src.pf) not ported") }
                if outImg == nil { outImg = Img(.rgba, src.w, src.h, src.meta, planes: [buf(16), buf(16), buf(16), buf(src.w * src.h)]) }
                gpu.run("k_fade_a", [src.p[3], outImg!.p[3]], [Int32(src.w * src.h), Int32(f), 0, 32768], grid: (src.w * src.h, 1))
                return Img(.rgba, src.w, src.h, src.meta, planes: [src.p[0], src.p[1], src.p[2], outImg!.p[3]])
            }
            guard src.pf == .yuv420 else { die("fade on \(src.pf) not ported") }
            if outImg == nil { outImg = Img(.yuv420, src.w, src.h, src.meta) }
            gpu.run("k_fade_a", [src.p[0], outImg!.p[0]], [Int32(src.w * src.h), Int32(f), 16, (16 << 16) + 32768], grid: (src.w * src.h, 1))
            for pi in 1...2 {
                let (pw, ph) = src.dims(pi)
                gpu.run("k_fade_c", [src.p[pi], outImg!.p[pi]], [Int32(pw * ph), Int32(f)], grid: (pw * ph, 1))
            }
            return outImg!
        }
    }

    func mixer(_ s: FSpec, _ n0: Node) -> Node {
        let keys = ["rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb"]
        for k in s.kv.keys where !keys.contains(k) { die("colorchannelmixer \(k) not ported") }
        var L = [Int32]()
        for k in keys {
            let c = s.kv[k].map { num($0) } ?? (k == "rr" || k == "gg" || k == "bb" ? 1 : 0)
            L += (0..<256).map { Int32(lrint(Double($0) * c)) }
        }
        let bl = bufOf(L)
        let n = need(n0, [.rgb], prefer: .rgb)
        var out: Img?
        return FuncNode([n], constant: true) { _, i in
            let src = i[0]
            if out == nil { out = Img(.rgb, src.w, src.h, src.meta) }
            gpu.run("k_mixer", [src.p[0], src.p[1], src.p[2], out!.p[0], out!.p[1], out!.p[2], bl], [Int32(src.w * src.h)], grid: (src.w * src.h, 1))
            return out!
        }
    }

    func lutrgb(_ s: FSpec, _ n0: Node) -> Node {
        for k in s.kv.keys where !["r", "g", "b"].contains(k) { die("lutrgb \(k) not ported") }
        let luts = ["r", "g", "b"].map { c -> MTLBuffer in
            let e = Expr.parse(s.kv[c] ?? "val")
            return lut256 { v in
                let r = e.eval(["val": Double(v), "maxval": 255, "minval": 0])
                return min(max(Int(r), 0), 255)
            }
        }
        let n = need(n0, [.rgb], prefer: .rgb)
        var out: Img?
        return FuncNode([n], constant: true) { _, i in
            let src = i[0]
            if out == nil { out = Img(.rgb, src.w, src.h, src.meta) }
            for pi in 0..<3 { gpu.run("k_lut", [src.p[pi], out!.p[pi], luts[pi]], [Int32(src.w * src.h)], grid: ((src.w * src.h + 3) / 4, 1)) }
            return out!
        }
    }

    func scroll(_ s: FSpec, _ n: Node) -> Node {
        for k in s.kv.keys where k != "vertical" && k != "v" { die("scroll \(k) not ported") }
        let vspeed = Float(num(s.kv["vertical"] ?? s.kv["v"] ?? "0"))
        var vpos: Float = .nan, frames = 0
        var out: Img?
        return FuncNode([n], sequential: true) { k, i in
            let src = i[0]
            if vpos.isNaN { vpos = (1 - 0) * Float(src.h) }
            precondition(k == frames, "scroll frames out of order")
            vpos = fmodf(vpos, Float(src.h))
            var v = Int(vpos)
            if v < 0 { v += src.h }
            // `v_pos += v_speed * height` contracts to fmaf
            vpos = fmaf(vspeed, Float(src.h), vpos)
            frames += 1
            if out == nil { out = Img(src.pf, src.w, src.h, src.meta) }
            for pi in 0..<src.p.count {
                let (pw, ph) = src.dims(pi)
                let sub = (src.pf == .yuv420 && pi > 0) ? 1 : 0
                let pv = (v + (1 << sub) - 1) >> sub
                gpu.run("k_scroll", [src.p[pi], out!.p[pi]], [Int32(pw), Int32(ph), Int32(pv), 0], grid: (pw, ph))
            }
            out!.meta = src.meta
            return out!
        }
    }

    func displace(_ s: FSpec, _ ins: [Node]) -> Node {
        guard s.kv["edge"] == "smear" else { die("displace \(s.kv) not ported") }
        var out: Img?
        return FuncNode(ins) { _, i in
            let src = i[0], xm = i[1], ym = i[2]
            guard src.pf == .rgb, xm.pf == .rgb, ym.pf == .rgb, xm.w == src.w, ym.h == src.h else { die("displace inputs not ported") }
            if out == nil { out = Img(.rgb, src.w, src.h, src.meta) }
            for pi in 0..<3 {
                gpu.run("k_displace", [src.p[pi], xm.p[pi], ym.p[pi], out!.p[pi]], [Int32(src.w), Int32(src.h)], grid: (src.w, src.h))
            }
            return out!
        }
    }

    func pad(_ s: FSpec, _ n: Node) -> Node {
        guard (s.opt("color", 4) ?? "black") == "black" else { die("pad color not ported") }
        var out: Img?
        return FuncNode([n]) { _, i in
            let src = i[0]
            guard src.pf == .yuv420 else { die("pad on \(src.pf) not ported") }
            var vars: [String: Double] = ["iw": Double(src.w), "ih": Double(src.h), "in_w": Double(src.w), "in_h": Double(src.h)]
            let W = Int(Expr.parse(s.opt("w", 0)!).eval(vars)), H = Int(Expr.parse(s.opt("h", 1)!).eval(vars))
            vars["ow"] = Double(W); vars["oh"] = Double(H); vars["out_w"] = Double(W); vars["out_h"] = Double(H)
            let x = Int(Expr.parse(s.opt("x", 2) ?? "0").eval(vars)) & ~1, y = Int(Expr.parse(s.opt("y", 3) ?? "0").eval(vars)) & ~1
            if out == nil {
                out = Img(.yuv420, W, H, src.meta)
                for pi in 0..<3 { let (pw, ph) = out!.dims(pi); fillPlane(out!.p[pi], pw * ph, pi == 0 ? 16 : 128) }
            }
            for pi in 0..<3 {
                let sub = pi == 0 ? 0 : 1
                let (pw, ph) = src.dims(pi), (ow, _) = out!.dims(pi)
                copyPlane(src.p[pi], out!.p[pi], w: pw, h: ph, sstride: pw, dstride: ow, dx: x >> sub, dy: y >> sub)
            }
            return out!
        }
    }
}

// A color= plate, materialised in whichever format its consumer asks for.
final class ColorNode: Node {
    let w: Int, h: Int, white: Bool
    var made: [PF: Img] = [:]
    init(w: Int, h: Int, white: Bool) { self.w = w; self.h = h; self.white = white; super.init(); constant = true; tb = 30 }
    func `as`(_ pf: PF) -> Img {
        if let m = made[pf] { return m }
        var meta = Meta()
        if pf == .rgba { meta.range = 1; meta.csp = 0 }
        let img = Img(pf, w, h, meta)
        for i in 0..<img.p.count {
            let (pw, ph) = img.dims(i)
            let v: Int
            switch pf {
            case .rgba: v = i == 3 ? 255 : (white ? 255 : 0)
            case .yuva420: v = i == 3 ? 255 : (i == 0 ? (white ? 235 : 16) : 128)
            default: die("color as \(pf) not ported")
            }
            fillPlane(img.p[i], pw * ph, v)
        }
        made[pf] = img
        return img
    }
    // a plate read directly (alphamerge's main) is RGB+alpha
    override func compute(_ n: Int) -> Img { self.as(.rgba) }
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------
final class VTWriter {
    let writer: AVAssetWriter, input: AVAssetWriterInput, adaptor: AVAssetWriterInputPixelBufferAdaptor
    let w: Int, h: Int
    init(path: String, w: Int, h: Int, quality: Double) {
        self.w = w; self.h = h
        try? FileManager.default.removeItem(atPath: path)
        writer = try! AVAssetWriter(outputURL: URL(fileURLWithPath: path), fileType: .mov)
        input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: w, AVVideoHeightKey: h,
            AVVideoCompressionPropertiesKey: [
                AVVideoQualityKey: quality,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                AVVideoExpectedSourceFrameRateKey: 30,
            ] as [String: Any],
        ])
        input.expectsMediaDataInRealTime = false
        adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h,
        ])
        writer.add(input)
        guard writer.startWriting() else { die("AVAssetWriter: \(writer.error.map { "\($0)" } ?? "?")") }
        writer.startSession(atSourceTime: .zero)
    }
    func append(_ y: UnsafePointer<UInt8>, _ uv: UnsafePointer<UInt8>, frame: Int) {
        var pbo: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &pbo)
        let pb = pbo!
        CVPixelBufferLockBaseAddress(pb, [])
        let yd = CVPixelBufferGetBaseAddressOfPlane(pb, 0)!.assumingMemoryBound(to: UInt8.self)
        let ys = CVPixelBufferGetBytesPerRowOfPlane(pb, 0)
        for r in 0..<h { memcpy(yd + r * ys, y + r * w, w) }
        let cd = CVPixelBufferGetBaseAddressOfPlane(pb, 1)!.assumingMemoryBound(to: UInt8.self)
        let cs = CVPixelBufferGetBytesPerRowOfPlane(pb, 1)
        let cw = (w + 1) / 2 * 2
        for r in 0..<(h + 1) / 2 { memcpy(cd + r * cs, uv + r * cw, cw) }
        CVPixelBufferUnlockBaseAddress(pb, [])
        while !input.isReadyForMoreMediaData { usleep(500) }
        if !adaptor.append(pb, withPresentationTime: CMTime(value: Int64(frame), timescale: 30)) {
            die("append failed: \(writer.error.map { "\($0)" } ?? "?")")
        }
    }
    func finish() {
        input.markAsFinished()
        let sem = DispatchSemaphore(value: 0)
        writer.finishWriting { sem.signal() }
        sem.wait()
        if writer.status != .completed { die("AVAssetWriter: \(writer.error.map { "\($0)" } ?? "?")") }
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
var args = Array(CommandLine.arguments.dropFirst())
func flag(_ name: String) -> Bool { if let i = args.firstIndex(of: name) { args.remove(at: i); return true }; return false }
func value(_ name: String) -> String? {
    if let i = args.firstIndex(of: name), i + 1 < args.count { let v = args[i + 1]; args.removeSubrange(i...i + 1); return v }
    return nil
}
let raw = flag("--raw")
let tap = value("--tap")
let frameLimit = value("--frames").map { Int($0)! }
guard args.count == 2 else { die("usage: bars_gpu PLAN.json OUT [--raw] [--tap LABEL] [--frames N]") }
let planData = try! Data(contentsOf: URL(fileURLWithPath: args[0]))
let plan = try! JSONSerialization.jsonObject(with: planData) as! [String: Any]
let fc = plan["fc"] as! String, inArgv = plan["in_argv"] as! [String], dur = plan["dur"] as! Double
let outPath = args[1]

let graph = Graph(fc: fc, specs: parseInputs(inArgv))
guard let outNode = graph.labels[tap ?? "vout"] else { die("no label [\(tap ?? "vout")]") }
// Nodes are asked for frames by pts in 1/30 (source frames by index before
// fps). The fps'd source bounds the stream: it starts at the first source
// frame's rounded pts and ends with it. A tap upstream of fps runs on
// source frames from 0.
func dependsOnFps(_ n: Node) -> FpsNode? {
    if let f = n as? FpsNode { return f }
    for i in n.inputs { if let f = dependsOnFps(i) { return f } }
    return nil
}
let fpsNode = dependsOnFps(outNode)
let firstPts = Int(fpsNode?.first ?? 0)
let count = fpsNode?.map.count ?? graph.srcTimeline.0.count
// `-r 30` (cfr) as the delivered encode runs it: the first frame is
// repeated back to pts 0, and `-t dur` ends the output
let cfr = tap == nil
var schedule: [Int] = (cfr ? Array(repeating: firstPts, count: firstPts) : []) + (0..<count).map { firstPts + $0 }
let tLimit = durationUs(String(format: "%.3f", dur))
schedule = Array(schedule.prefix { _ in true }.enumerated().filter { Int64($0.offset) * 1_000_000 < tLimit * 30 }.map { $0.element })
if let f = frameLimit { schedule = Array(schedule.prefix(f)) }
let total = schedule.count

FileManager.default.createFile(atPath: outPath, contents: nil)
let fh = raw ? FileHandle(forWritingAtPath: outPath)! : nil
var vt: VTWriter?
var pending: [(MTLCommandBuffer, [MTLBuffer], Int, Img)] = []
var stage: [[MTLBuffer]] = []
var printedFmt = false
var gpuBusy = 0.0

func drain(_ upTo: Int) {
    while pending.count > upTo {
        let (cb, bufs, n, img) = pending.removeFirst()
        cb.waitUntilCompleted()
        if let e = cb.error { die("GPU: \(e)") }
        gpuBusy += cb.gpuEndTime - cb.gpuStartTime
        if raw {
            for (k, b) in bufs.enumerated() {
                let (pw, ph) = img.dims(img.dumpOrder[k])
                fh!.write(Data(bytesNoCopy: b.contents(), count: pw * ph, deallocator: .none))
            }
        } else {
            vt!.append(bufs[0].contents().assumingMemoryBound(to: UInt8.self), bufs[1].contents().assumingMemoryBound(to: UInt8.self), frame: n)
        }
    }
}

let t0 = Date()
for n in 0..<total {
    gpu.begin()
    let img = outNode.get(schedule[n])
    if !printedFmt {
        FileHandle.standardError.write("bars_gpu: [\(tap ?? "vout")] \(img.ffName) \(img.w)x\(img.h), \(total) frames\n".data(using: .utf8)!)
        printedFmt = true
    }
    let slot = n % 3
    if stage.count <= slot {
        if raw { stage.append(img.dumpOrder.map { let (pw, ph) = img.dims($0); return buf(pw * ph) }) }
        else { stage.append([buf(img.w * img.h), buf(((img.w + 1) / 2) * ((img.h + 1) / 2) * 2)]) }
    }
    let sb = stage[slot]
    if raw {
        for (k, pi) in img.dumpOrder.enumerated() { let (pw, ph) = img.dims(pi); copyPlane(img.p[pi], sb[k], w: pw, h: ph, sstride: pw, dstride: pw) }
    } else {
        guard img.pf == .yuv420 else { die("VideoToolbox output needs yuv420p, got \(img.pf)") }
        if vt == nil {
            guard let q = plan["vt_quality"] as? Double else { die("plan has no vt_quality") }
            vt = VTWriter(path: outPath, w: img.w, h: img.h, quality: q)
        }
        copyPlane(img.p[0], sb[0], w: img.w, h: img.h, sstride: img.w, dstride: img.w)
        let cn = ((img.w + 1) / 2) * ((img.h + 1) / 2)
        gpu.run("k_nv12", [img.p[1], img.p[2], sb[1]], [Int32(cn)], grid: (cn, 1))
    }
    pending.append((gpu.end(), sb, n, img))
    drain(1)
}
drain(0)
fh?.closeFile()
vt?.finish()
for (k, v) in gpu.profT.sorted(by: { $0.value > $1.value }).prefix(40) {
    FileHandle.standardError.write(String(format: "%8.3fs  %@\n", v, k).data(using: .utf8)!)
}
FileHandle.standardError.write(String(format: "bars_gpu: %d frames in %.2fs (GPU busy %.2fs)\n", total, Date().timeIntervalSince(t0), gpuBusy).data(using: .utf8)!)
FileHandle.standardError.write(String(format: "bars_gpu: %d dispatches, %d barriers\n", gpu.dispatches, gpu.barriers).data(using: .utf8)!)
