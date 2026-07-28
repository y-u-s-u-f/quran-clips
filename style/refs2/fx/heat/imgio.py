import numpy as np, subprocess
def save_png(arr, path):
    """arr: (H,W,3) uint8"""
    h,w,_=arr.shape
    p=subprocess.run(["ffmpeg","-v","error","-y","-f","rawvideo","-pix_fmt","rgb24",
        "-s",f"{w}x{h}","-i","-","-frames:v","1",path],input=arr.tobytes())
    return p.returncode
def heat_rgb(v, lo, hi):
    """diverging blue-white-red"""
    t=np.clip((v-lo)/(hi-lo),0,1)
    r=np.clip(1.5*t-0.25,0,1); b=np.clip(1.25-1.5*t,0,1)
    g=1-np.abs(2*t-1)
    return (np.stack([r,g,b],-1)*255).astype(np.uint8)
def upscale(a,fy,fx):
    return np.repeat(np.repeat(a,fy,0),fx,1)
