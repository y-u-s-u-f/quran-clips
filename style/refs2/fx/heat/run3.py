import numpy as np
from edge2 import analyse
R="/Users/yusuf/quran-clips/style/refs2"
# MATCHED GEOMETRY: 130-column window, 165 frames, so every number is comparable.
NC, NF = 130, 165
def m(name,path,ey,x0,ss):
    o,d,r,g=analyse(name,path,ey,x0,x0+NC,NF,ss)
    return o
print("--- calibration: our pipeline, known amplitude, 130-col window ---")
cal={}
for a in [0,0.5,1,2,3,4]:
    vs=[]
    for ey in (586,686):
        o=m(f"amp{a}",f"h_{a}.mp4",ey,165,1.0); vs.append((o['rms_total'],o['rms_resid']))
    cal[a]=(np.mean([v[0] for v in vs]), np.mean([v[1] for v in vs]))
    print(f"  amp +-{a:>4} px : rms_total {cal[a][0]:.3f}  rms_resid {cal[a][1]:.3f}")
print("\n--- references, same 130-col window, several windows ---")
refs=[]
for lbl,path,eys,ss in [("rose",f"{R}/ig_DNrnY9b2EHe.mp4",(576,699),880/30),
                        ("rose2",f"{R}/ig_DNrnY9b2EHe.mp4",(576,699),410/30),
                        ("rose3",f"{R}/ig_DNrnY9b2EHe.mp4",(576,699),185/30),
                        ("gold",f"{R}/ig_DNon3t8J9je.mp4",(577,701),30/30)]:
    for ey in eys:
        for x0 in (110,200,290):
            o=m(f"{lbl}",path,ey,x0,ss)
            if o['cols']<100: continue
            refs.append((o['rms_total'],o['rms_resid']))
            print(f"  {lbl:6s} y={ey} x={x0:3d} cols{o['cols']:4d}  rms_total {o['rms_total']:.3f}  rms_resid {o['rms_resid']:.3f}")
rt=np.array([x[0] for x in refs]); rr=np.array([x[1] for x in refs])
print(f"\n  REF MEAN  rms_total {rt.mean():.3f} (sd {rt.std():.3f})   rms_resid {rr.mean():.3f} (sd {rr.std():.3f})")
amps=np.array([0.5,1,2,3,4])
tt=np.array([cal[a][0] for a in amps]); rrc=np.array([cal[a][1] for a in amps])
kt=np.polyfit(amps,tt,1); kr=np.polyfit(amps,rrc,1)
print(f"  calib slopes: rms_total = {kt[0]:.3f}*amp + {kt[1]:.3f} ;  rms_resid = {kr[0]:.3f}*amp + {kr[1]:.3f}")
print(f"  => implied ref amplitude from rms_total : {(rt.mean()-kt[1])/kt[0]:.2f} px")
print(f"  => implied ref amplitude from rms_resid : {(rr.mean()-kr[1])/kr[0]:.2f} px")
print(f"  noise floor (amp 0): rms_total {cal[0][0]:.3f}  rms_resid {cal[0][1]:.3f}")
