# ruff: noqa: F821
import getpass
import os
from tempfile import mkdtemp

# Test if we are running in a docker
if getpass.getuser() == "jovyan":
    c.ServerApp.ip = "0.0.0.0"  # noqa S104

c.ServerApp.port = 8889
c.ServerApp.port_retries = 0
c.ServerApp.open_browser = False
# Add test helpers extension shipped with JupyterLab.
# You can replace the following line by the two following one
#   import jupyterlab
#   c.LabServerApp.extra_labextensions_path = str(Path(jupyterlab.__file__).parent / "galata")  # noqa: E501
# c.LabServerApp.extra_labextensions_path = str(Path(__file__).parent)

c.LabApp.workspaces_dir = mkdtemp(prefix="galata-workspaces-")

c.ServerApp.root_dir = os.environ.get(
    "JUPYTERLAB_GALATA_ROOT_DIR", mkdtemp(prefix="galata-test-")
)
c.IdentityProvider.token = ""
c.ServerApp.password = ""
c.ServerApp.disable_check_xsrf = True
c.LabApp.expose_app_in_browser = True
