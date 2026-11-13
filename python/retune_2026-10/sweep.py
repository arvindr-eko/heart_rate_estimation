"""One-factor-at-a-time sweep on the TUNE SUBSET (subset of TRAIN only). Objective J = mean over 5 bands of
(correct% - 3*wrong%) of ticks; also prints TTFR. Usage: sweep.py <label> <json variants {name:[defs]}> [full]"""
import sys, json, evalkit as E, bands, numpy as np
SRC=r"C:\Users\daniel\Desktop\pcg_abab\v0.16"
def J(res, rows):
    js=[]; per={}
    for n,lo,hi in bands.BANDS:
        rr=[r for r in rows if lo<=float(r["bpm"])<hi]
        o,_=E.summarize(res,rr); j=100*(o["correct"]-3*o["wrong_displayed"]); js.append(j); per[n]=round(j,1)
    return float(np.mean(js)), per
if __name__=="__main__":
    label=sys.argv[1]; V=json.load(open(sys.argv[2])); full=len(sys.argv)>3
    rows=E.load_split()
    sub=[r for r in rows if r["split"]=="train"] if full else E.tune_subset(rows)
    files=[r["file"] for r in sub]
    out=[]
    for name,defs in V.items():
        d=E.build(SRC,f"{label}_{name}",defs); res=E.run(d,files,f"{label}_{name}")
        o,_=E.summarize(res,sub); j,per=J(res,sub)
        print(f"{name:28s} J={j:6.2f} {per}  corr={o['correct']*100:5.1f} wrong={o['wrong_displayed']*100:4.2f} nodisp={o['no_display']*100:5.1f} ttfr={o['ttfr_median']} never={o['never_correct']*100:4.1f}",flush=True)
        out.append((name,defs,j,per,o))
    json.dump([(n,d,j,p) for n,d,j,p,o in out],open(f"sweep_{label}.json","w"))
