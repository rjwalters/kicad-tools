"""New-style CLI commands implementing the Command protocol.

Modules in this package are auto-discovered by the registry.
Each module should export a class that implements the Command protocol
(name, help, add_arguments, run).

During the incremental migration, new-style commands coexist with
legacy commands defined in parser.py and dispatched via __init__.py.
"""
