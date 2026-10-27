# Application entry points

This directory records deployable Web application ownership.  The shared Vite
implementation remains at `web_viewer/app` for source compatibility; each
subdirectory declares the entry mode and service dependencies used by Docker
and the validated package builder.  Canonical data is never owned here.
