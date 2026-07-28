import numpy as np, sys
from rd import read_gray
from imgio import save_png, upscale
# y-t slice: column x through the upper caption bar; y 555..645; 150 frames; 4x vertical zoom
def tile(path, col=225, y0=555, y1=645, n=150, ss=1.0, zx=4, zy=4):
    F = read_gray(path, col, y0, 2, y1-y0, n, ss)[:, :, 0].T   # (rows, T)
    a = np.stack([F]*3, -1).astype(np.uint8)
    return upscale(a, zy, zx)
if __name__=="__main__":
    items=[s.split("=") for s in sys.argv[1].split(",")]
    out=sys.argv[2]
    col=int(sys.argv[3]) if len(sys.argv)>3 else 225
    tiles=[tile(p,col=col) for _,p in items]
    H=max(t.shape[0] for t in tiles)
    rows=[]
    for (n,_),t in zip(items,tiles):
        pad=np.zeros((H,t.shape[1],3),np.uint8); pad[:t.shape[0]]=t
        rows.append(pad); rows.append(np.full((8,t.shape[1],3),(255,60,60),np.uint8))
    save_png(np.concatenate(rows,0), out)
    print("wrote",out,[i[0] for i in items])
