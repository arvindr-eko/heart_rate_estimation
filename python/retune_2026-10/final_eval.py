import sys, json, pickle, numpy as np, evalkit as E, bands
V15=r"C:\Users\daniel\Desktop\pcg_abab\v0.15"; V16=r"C:\Users\daniel\Desktop\pcg_abab\v0.16"
if __name__=="__main__":
    rows=E.load_split()
    CFG=[("v0.15 (as shipped)",V15,"fin_v015",[]),
         ("v0.15 + new templates",V16,"fin_tpl",["SLOW_RATIO_MIN_OVERRIDE=1.5f"]),
         ("v0.16 (templates + slow ratio 1.35)",V16,"fin_v016",[])]
    sets={"test":[r for r in rows if r["split"]=="test"],"train":[r for r in rows if r["split"]=="train"]}
    out={}
    for label,src,name,defs in CFG:
        d=E.build(src,name,defs,outdir=V16+r"\drivers")
        for sn,rs in sets.items():
            res=E.run(d,[r["file"] for r in rs],f"{name}_{sn}")
            out[(label,sn)]=res
            print(f"== {label} / {sn}"); bands.by_band(res,rs,"")
    pickle.dump(out,open("final_results.pkl","wb"),protocol=4)
