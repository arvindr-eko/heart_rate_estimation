import sys, numpy as np
sys.path.insert(0, r"C:\Users\daniel\Desktop\pcg_abab")
import synth_pcg as SP
OUT = r"C:\Users\daniel\Desktop\pcg_abab\v0.15\synth"
for hr in [30, 33, 36, 40, 45, 50, 60]:
    for white in (0.18, 0.35):
        for jit in (0.01, 0.06):
            p = SP.CLEAN(); p['fs']=4000; p['dur']=60.0; p['hr']=hr; p['white']=white; p['jitter']=jit; p['seed']=0
            sig, true_hr = SP.synth(p)
            pcm = np.clip(sig*6000.0, -32000, 32000).astype(np.int16).astype(np.float32)
            pcm.tofile(f"{OUT}\hr{hr:03d}_w{white:.2f}_j{jit:.2f}.f32")
print("done")
