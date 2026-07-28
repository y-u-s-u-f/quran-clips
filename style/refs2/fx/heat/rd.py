import numpy as np, subprocess
def read_gray(path, x,y,w,h, n, ss=0.0):
    cmd=["ffmpeg","-v","error","-ss",str(ss),"-i",path,
         "-vf",f"format=gray,crop={w}:{h}:{x}:{y}",
         "-frames:v",str(n),"-f","rawvideo","-pix_fmt","gray","-"]
    raw=subprocess.run(cmd,capture_output=True).stdout
    m=len(raw)//(w*h)
    assert m>0 and len(raw)%(w*h)==0, (len(raw), w*h, len(raw)%(w*h))
    return np.frombuffer(raw[:m*w*h],dtype=np.uint8).reshape(m,h,w).astype(np.float32)
