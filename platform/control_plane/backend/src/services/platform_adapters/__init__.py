"""One module per governed platform, each a thin wrapper over the service that owns the behaviour;
the protocol and its error types live in ``services/platform_adapter.py``.

A PACKAGE, not two more modules beside forty others: the adapters are a closed set that grows by
one file per platform, and the directory is what makes "which platforms does AGP govern?" a
``ls`` rather than a grep. It also keeps the import direction honest — an adapter imports its
platform's service, and NOTHING here imports a sibling adapter. A cross-import would be the
inversion this seam was built to remove (``AgentIdentityService`` dispatching to the Databricks
one), rebuilt one layer up.

Deliberately EMPTY of re-exports. Naming the adapters here would make importing either one drag
in both platforms' service graphs — the cost ``services/platform_adapter.py`` avoids by keeping
the factory out of the seam module.
"""
