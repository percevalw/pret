from pret.bridge import js, pyodide
from pret.ui.react import use_effect


def use_body_style(styles):
    def apply_styles():
        # Remember the original styles
        original_styles = {}
        for key, value in styles.items():
            original_styles[key] = getattr(js.document.documentElement.style, key, "")
            setattr(js.document.documentElement.style, key, value)

        # Cleanup function to revert back to the original styles
        def cleanup():
            for key, value in original_styles.items():
                setattr(js.document.documentElement.style, key, value)

        return cleanup

    use_effect(pyodide.ffi.create_once_callable(apply_styles), [str(styles)])
