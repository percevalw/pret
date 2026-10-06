"""
Expose an isolated local JupyterLab application to the shared Playwright fixture
"""

import os
import sys
from pathlib import Path

import jupyterlab

c = get_config()  # noqa F821
c.ServerApp.ip = "127.0.0.1"
c.ServerApp.port_retries = 0
c.ServerApp.open_browser = False
c.ServerApp.root_dir = os.environ["PRET_TEST_ROOT"]
c.ServerApp.token = ""
c.IdentityProvider.token = ""
c.ServerApp.password = ""
c.ServerApp.disable_check_xsrf = True
c.LabApp.workspaces_dir = str(Path(os.environ["PRET_TEST_DATA"]) / "workspaces")
c.LabApp.user_settings_dir = str(Path(os.environ["PRET_TEST_DATA"]) / "settings")
c.LabApp.expose_app_in_browser = True
# Include component extensions from the runner environment when Jupyter uses a separate interpreter
c.LabServerApp.labextensions_path = [
    *[str(Path(path) / "labextensions") for path in os.environ["JUPYTER_PATH"].split(os.pathsep)],
    str(Path(sys.prefix) / "share/jupyter/labextensions"),
]
c.LabServerApp.extra_labextensions_path = str(Path(jupyterlab.__file__).parent / "galata")
# Kernels installed system wide would make JupyterLab ask which kernel a new notebook uses
c.KernelSpecManager.allowed_kernelspecs = {
    path.name for path in (Path(os.environ["PRET_TEST_DATA"]) / "kernels").iterdir()
}
# Extensions installed system wide can ask for a JupyterLab rebuild, its dialog would cover the tested pages
c.ServerApp.tornado_settings = {"page_config_data": {"buildCheck": False, "buildAvailable": False}}
