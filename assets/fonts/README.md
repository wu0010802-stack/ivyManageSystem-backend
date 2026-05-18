# PDF 字型

## NotoSansTC-Regular.ttf

Noto Sans TC Regular（Google Fonts，SIL OFL 1.1 授權，可商用）。
原始字型來源：https://fonts.google.com/noto/specimen/Noto+Sans+TC

### 為什麼是子集化版本

原始 Noto Sans TC Regular VF 為 **11.9 MB**，包含本系統用不到的：
- CJK Unified Ideographs Extension B（1707 字符，台灣現代姓名極少用）
- variable font axes data（`gvar` 3.2 MB；我們只用 Regular weight 不需要）
- 直排相關 OpenType features（`vert`/`vrt2`/`vhal`/`vpal` 等）
- DSIG 數位簽章 table

bundle 版為 pyftsubset 處理後的 **5.7 MB**（省 49.9%），保留：
- 全 BMP CJK Unified Ideographs（U+4E00-9FFF，15383 字符）
- CJK Extension A（U+3400-4DBF，575 字符，含台灣戶政罕用人名字）
- 拉丁基本 + 標點 + 全形/半形 + CJK 標點 + 部分符號

驗證涵蓋的罕用姓名字：凃、淼、鑫、垚、翊、愷、茜、婭、妍、懿、婷、瑄、佑、承、羽

### 重做 subset

需要 fonttools：

```bash
pip install fonttools
```

從 [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+TC) 下載原檔 `NotoSansTC-Regular.ttf`（VF 版），放到本目錄改名為 `NotoSansTC-Regular.ttf.orig`，然後：

```bash
cd assets/fonts
pyftsubset NotoSansTC-Regular.ttf.orig \
  --output-file=NotoSansTC-Regular.ttf \
  --unicodes="U+0020-007E,U+00A0-00FF,U+2000-206F,U+2100-218F,U+2460-24FF,U+25A0-25FF,U+2600-26FF,U+3000-303F,U+3040-309F,U+30A0-30FF,U+3100-312F,U+3200-32FF,U+3400-4DBF,U+4E00-9FFF,U+F900-FAFF,U+FE30-FE4F,U+FF00-FFEF" \
  --layout-features-=vert,vrt2,vrt3,vhal,vpal,vkrn,vkna \
  --no-hinting \
  --drop-tables+=DSIG,fvar,gvar,avar,HVAR,VVAR,MVAR,STAT \
  --notdef-outline \
  --recommended-glyphs \
  --name-IDs="1,2,3,4,5,6" \
  --name-legacy
```

驗證新版：

```bash
python -m pytest tests/test_pdf_bold_font_embed.py -v
python -m pytest tests/ -k "pdf or roll or roster or salary_slip or pos_receipt or iep or growth_report or attendance_sheet or enrollment_cert"
```

### 何時需要重做

- 增加 PDF 場景且該場景會印出非常罕用的字（落在 Extension B 範圍）
- 升級 Noto Sans TC 版本
- 需要 Bold weight（另 bundle `NotoSansTC-Bold.ttf` 子集，並更新 `utils/pdf_fonts.py` 的 family map）
