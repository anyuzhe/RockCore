"""Create Windows executable version metadata from VERSION."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
parts = [int(part) for part in version.split(".")]
while len(parts) < 4:
    parts.append(0)
version_tuple = tuple(parts[:4])
version_text = ".".join(str(part) for part in version_tuple)
output = f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple}, prodvers={version_tuple}, mask=0x3f,
    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '浙江岩创科技有限公司'),
        StringStruct('FileDescription', 'RockCore 多 AI 智能工程工作台'),
        StringStruct('FileVersion', '{version_text}'),
        StringStruct('InternalName', 'RockCore'),
        StringStruct('OriginalFilename', 'RockCore.exe'),
        StringStruct('ProductName', 'RockCore'),
        StringStruct('ProductVersion', '{version_text}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
'''
(ROOT / "build" / "version_info.generated.txt").write_text(output, encoding="utf-8")
