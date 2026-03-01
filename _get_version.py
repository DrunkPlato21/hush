import re
m = re.search(r"__version__ = '(.+?)'", open('hush.py').read())
print(m.group(1) if m else '')
