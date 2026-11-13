import sys, evalkit as E, bands
if __name__ == '__main__':
    rows=E.load_split(); sub=E.tune_subset(rows); files=[r["file"] for r in sub]
    print("tune subset", len(files))
    V15=r"C:\Users\daniel\Desktop\pcg_abab\v0.15"; V16=r"C:\Users\daniel\Desktop\pcg_abab\v0.16"
    cfg=[("base_v015",V15,[]),("newtpl",V16,[]),("newtpl_unsc0",V16,["TEMPLATE_UNSCORED_BELOW_BPM_OVERRIDE=0.0f"])]
    for n,src,defs in cfg:
        d=E.build(src,n,defs,outdir=V16+r"\drivers"); res=E.run(d,files,n)
        bands.by_band(res,sub,n)
