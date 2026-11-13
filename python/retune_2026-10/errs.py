import sys, numpy as np, collections, evalkit as E
if __name__=="__main__":
    name=sys.argv[1]
    rows=E.load_split(); sub=E.tune_subset(rows); files=[r["file"] for r in sub]
    d=r"C:\Users\daniel\Desktop\pcg_abab\v0.16\drivers\%s.js"%name
    res=E.run(d,files,name)
    for lo,hi in ((30,40),(40,60),(60,100),(100,160),(160,300)):
        rat=[]; perfile=collections.Counter(); n=0
        for r in sub:
            gt=float(r["bpm"])
            if not lo<=gt<hi: continue
            t,b,_=res[r["file"]]
            for x in b:
                n+=1
                if np.isnan(x): continue
                if E.classify(x,gt)!="correct": rat.append(x/gt); perfile[r["file"]]+=1
        h=np.histogram(rat,bins=[0,0.4,0.55,0.8,0.95,1.05,1.2,1.4,1.6,1.9,2.1,2.6,3.1,10])[0] if rat else []
        print(f"{lo}-{hi}: wrong ticks {len(rat)}/{n} in {len(perfile)} files; ratio hist [<.4,.4-.55,.55-.8,.8-.95,.95-1.05,1.05-1.2,1.2-1.4,1.4-1.6,1.6-1.9,1.9-2.1,2.1-2.6,2.6-3.1,>3.1]={list(h)}")
