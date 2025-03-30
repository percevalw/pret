PID_PATH=/tmp/pret-jupyter-galata-app.pid
# Check node >= 20 (i'm not sure this is
# strictly necessary, but I think it will avoid a few headaches)
NodeVersion=$(node --version)
if [ "$(echo "${NodeVersion}\nv20" | sort -V|tail -1)" != "${NodeVersion}" ]; then
  echo "Node >= 20 is required"
  exit 1
fi

# Used to put playwrigth reports in a separate folder
export PYTHON_VERSION=$(python --version | cut -d' ' -f2)

# Start Jupyter Lab, save the PID to stop it later, and store logs
export JUPYTERLAB_GALATA_ROOT_DIR=tests/jupyter
jupyter --version
jupyter lab --config galata_config.py > /tmp/jupyter.log 2>&1 &
echo $! > $PID_PATH

# Wait for Jupyter Lab to start (check for "is running at:" in the logs)
while ! grep -q "is running at:" /tmp/jupyter.log; do
  # Check the app is running
  if ! kill -0 $(cat $PID_PATH) 2>/dev/null; then
    echo "Jupyter Lab failed to start"
    cat /tmp/jupyter.log
    exit 1
  fi
  sleep 1
done

# Run the tests
jlpm playwright test tests/jupyter

# Store return code
RET_CODE=$?

# Stop Jupyter Lab
kill $(cat $PID_PATH)

# Return the return code
exit $RET_CODE
