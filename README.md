# Tripo API Workbench - Colab

Chay trong Google Colab:

```python
!pip -q install gradio requests
!python /content/TripoAPI/colab/tripo_colab.py
```

Neu muon share moi notebook:

1. Upload `colab/tripo_colab.py` len GitHub public repo.
2. Lay raw URL dang:

```text
https://raw.githubusercontent.com/<user>/<repo>/<branch>/colab/tripo_colab.py
```

3. Trong notebook, them truoc cell chay app:

```python
import os
os.environ["TRIPO_COLAB_RAW_URL"] = "https://raw.githubusercontent.com/<user>/<repo>/<branch>/colab/tripo_colab.py"
```

Notebook se tu tai script ve `/content/tripo_colab.py`.

Mac dinh history/cache nam o:

```text
/content/tripo_colab
```

Muon luu vao Drive:

```python
from google.colab import drive
drive.mount('/content/drive')
import os
os.environ["TRIPO_COLAB_HOME"] = "/content/drive/MyDrive/TripoAPI"
```

Sau do chay lai app.
