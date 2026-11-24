# PyInstaller runtime hook: configure HTTPS trust before requests or launcher modules load.
from tls_bootstrap import configure_tls_bundle

configure_tls_bundle()
