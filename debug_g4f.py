import g4f
print("g4f version:", g4f.version)
print("Providers in g4f.Provider:")
for item in dir(g4f.Provider):
    if not item.startswith("__"):
        print(item)
