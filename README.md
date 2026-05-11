# Tripo API Workbench - Colab

Notebook `TripoAPI_Colab.ipynb` gom 2 cell:

```python
!pip -q install gradio requests trimesh
```

```python
from pathlib import Path
from google.colab import drive
drive.mount("/content/drive")
import os
os.environ["TRIPO_COLAB_HOME"] = "/content/drive/MyDrive/TripoAPI"
import requests

url = "https://raw.githubusercontent.com/jofix2004/tripo-colab-workbench/master/tripo_colab.py"
script = Path("/content/tripo_colab.py")
script.write_text(requests.get(url, timeout=60).text, encoding="utf-8")
exec(script.read_text(encoding="utf-8"), globals())
```

Share notebook la du.

Mac dinh history/cache nam o Drive:

```text
/content/drive/MyDrive/TripoAPI
```
