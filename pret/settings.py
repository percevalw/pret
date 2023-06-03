settings = {
    'pickler': {
        'pickled_modules': [
            "pret",
            #"pret.serialize",
            "pret.state",
            "pret.render",
            "pret.bridge",
            "pret.manager",
            "pret.stubs",
            "pret.stubs.*",
        ],
        'recurse': True,
        'save_code_as_source': "auto",
    }
}