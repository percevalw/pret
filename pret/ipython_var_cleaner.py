class EnvAutoCleaner(object):
    def __init__(self, ip):
        self.shell = ip
        self.cell_mapping = {}
        self.dunder_indices = {"_": None, "__": None, "___": None}

    def pre_run_cell(self, info):
        shell = self.shell
        old_idx = self.cell_mapping.get(info.cell_id)
        if old_idx is not None:
            try:
                del shell.user_ns[f"_{old_idx}"]
            except KeyError:
                pass
            try:
                del shell.user_ns_hidden[f"_{old_idx}"]
            except KeyError:
                pass
            try:
                del shell.history_manager.output_hist[old_idx]
            except KeyError:
                pass
            if old_idx == self.dunder_indices["_"]:
                shell.user_ns["_"] = None
                shell.user_ns_hidden["_"] = None
                shell.displayhook._ = None
                if shell.displayhook.exec_result is not None:
                    shell.displayhook.exec_result.result = None
                if shell.last_execution_result is not None:
                    shell.last_execution_result.result = None
            elif old_idx == self.dunder_indices["__"]:
                shell.user_ns["__"] = None
                shell.user_ns_hidden["__"] = None
                shell.displayhook.__ = None
            elif old_idx == self.dunder_indices["___"]:
                shell.user_ns["___"] = None
                shell.user_ns_hidden["___"] = None
                shell.displayhook.___ = None

        self.cell_mapping[info.cell_id] = self.shell.execution_count

        # TODO: handle case where I comment the code of a cell
        #       and execute (ie erase output)

    def post_run_cell(self, result):
        if result.result is not None:
            # shift dunder indices
            self.dunder_indices = {
                "_": self.shell.execution_count - 1,
                "__": self.dunder_indices["_"],
                "___": self.dunder_indices["__"],
            }


try:
    from IPython import get_ipython

    cleaner = EnvAutoCleaner(get_ipython())
    get_ipython().events.register("pre_run_cell", cleaner.pre_run_cell)
    get_ipython().events.register("post_run_cell", cleaner.post_run_cell)
except (ImportError, AttributeError):
    pass
