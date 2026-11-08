"""On-device algorithm: the Python reference of the stethoscope's heart-rate estimator, ported
one module to one C file. Per 50 ms block of 200 PCG samples:

frontend (A) -> net (B) -> features (C) -> scorer (D) -> tracker (E) -> readout (F)
                    +--> rhythm (R) -----------------------------------------^

Each module holds init and per-block or per-frame functions with fixed-size float32 state;
estimator composes them behind one call per block. Device modules never import training code.
"""
