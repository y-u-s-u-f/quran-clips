import numpy as np, sys, json
from rd import read_gray

def edge_disp(path, ey, x0, x1, nfr, ss, half=5):
    """Per-frame, per-column sub-pixel displacement of a horizontal edge,
    via 3-parameter least squares  I_t - T = -d*T' + a*T + b  (d in px)."""
    y0 = ey-half-1; h = 2*half+3
    F = read_gray(path, x0, y0, x1-x0, h, nfr, ss).astype(np.float64)  # (T,h,W)
    T,H,W = F.shape
    Tm = F.mean(0)                       # template (h,W)
    Ty = np.gradient(Tm, axis=0)         # (h,W)
    sl = slice(1,H-1)
    Tm_,Ty_ = Tm[sl],Ty[sl]
    d = np.zeros((T,W)); res=np.zeros((T,W))
    ones = np.ones(Tm_.shape[0])
    for x in range(W):
        A = np.stack([-Ty_[:,x], Tm_[:,x], ones],1)     # (h,3)
        AtA = A.T@A
        try: inv = np.linalg.inv(AtA + 1e-9*np.eye(3))
        except np.linalg.LinAlgError: continue
        B = (F[:,sl,x] - Tm_[None,:,x])                  # (T,h)
        sol = (inv @ (A.T @ B.T)).T                      # (T,3)
        d[:,x] = sol[:,0]
        res[:,x] = np.sqrt(((B - sol@A.T)**2).mean(1))
    grad = np.abs(Ty_).max(0)
    return d, grad, res

def analyse(name, path, ey, x0, x1, nfr=240, ss=5.0, half=5, verbose=False):
    d, grad, res = edge_disp(path,ey,x0,x1,nfr,ss,half)
    good = grad > 0.5*np.median(grad)
    good &= np.abs(d).max(0) < 6.0
    d = d[:,good]; T,W = d.shape
    glob = d.mean(1); stat = d.mean(0)
    r = d - glob[:,None] - stat[None,:] + d.mean()
    def sac(l):
        if l>=W: return float('nan')
        return float(np.corrcoef(r[:,:-l].ravel(), r[:,l:].ravel())[0,1])
    def tac(l): return float(np.corrcoef(r[:-l].ravel(), r[l:].ravel())[0,1])
    gd = glob-glob.mean()
    fr = np.fft.rfftfreq(T, 1/30.)
    P = (np.abs(np.fft.rfft(r-r.mean(0),axis=0))**2).mean(1)
    lowf = float(P[(fr>0)&(fr<2)].sum()/P[fr>0].sum())
    Gp = np.abs(np.fft.rfft(gd))**2
    glowf = float(Gp[(fr>0)&(fr<2)].sum()/Gp[fr>0].sum())
    return dict(name=name, cols=int(W), frames=int(T),
        rms_total=round(float(d.std()),4),
        rms_global=round(float(gd.std()),4), rms_resid=round(float(r.std()),4),
        sac1=round(sac(1),3), sac2=round(sac(2),3), sac4=round(sac(4),3),
        sac8=round(sac(8),3), sac16=round(sac(16),3),
        tac1=round(tac(1),3), tac2=round(tac(2),3), tac4=round(tac(4),3), tac8=round(tac(8),3),
        gtac1=round(float(np.corrcoef(gd[:-1],gd[1:])[0,1]),3),
        resid_lowfreq_frac=round(lowf,3), global_lowfreq_frac=round(glowf,3),
        fitres=round(float(res.mean()),3)), d, r, glob

if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("name");ap.add_argument("path");ap.add_argument("ey",type=int)
    ap.add_argument("x0",type=int);ap.add_argument("x1",type=int)
    ap.add_argument("--n",type=int,default=240);ap.add_argument("--ss",type=float,default=5.0)
    ap.add_argument("--npy",default=None)
    a=ap.parse_args()
    o,d,r,g=analyse(a.name,a.path,a.ey,a.x0,a.x1,a.n,a.ss)
    if a.npy: np.savez(a.npy,d=d,r=r,g=g)
    print(json.dumps(o))
