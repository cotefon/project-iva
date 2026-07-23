from markitdown import MarkItDown
import os
from dotenv import load_dotenv

load_dotenv()

# FILE = os.getenv("docs/CARPETA TRIBUTARIA FRUTAM.pdf")

md = MarkItDown(enable_plugins=False)  # Set to True to enable plugins
result = md.convert("docs/CARPETA TRIBUTARIA FRUTAM.pdf")

with open("output.md", "w", encoding="utf-8") as file:
    file.write(str(result))


# print(result.text_content)
