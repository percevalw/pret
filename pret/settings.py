settings = {
    "pickler": {
        "pickled_modules": [
            "pret",
            # "pret.serialize",
            "pret.state",
            "pret.render",
            "pret.bridge",
            "pret.manager",
            "pret.hooks",
            "pret.ui.*",
        ],
        "non_pickled_modules": [
            "pret.serialize",
        ],
        "recurse": True,
        "save_code_as_source": "auto",
    }
}
