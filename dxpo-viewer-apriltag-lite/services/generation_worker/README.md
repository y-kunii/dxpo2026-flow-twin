# Generation worker

Owns long-running COLMAP, masking, AprilTag metric-alignment, MRP, and
LichtFeld-input jobs.  Runtime implementation remains in
`generation_pipeline` while compatibility imports are stabilized.  HTTP
routers must enqueue or supervise this worker and must not execute GPU stages
inside the ASGI event loop.
