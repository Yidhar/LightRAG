"""Probe docling's PDF image extraction API.

Runs on one of the Phase 1 test PDFs with ``generate_picture_images=True``
and prints what we get back so the Phase 5 code can commit to a specific
shape. Nothing here persists — stdout only.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

PDF = Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else r"C:\Users\yidhar\Downloads\RCArtBook.pdf"
)
MAX_PICTURES = int(sys.argv[2]) if len(sys.argv) > 2 else 5


def main() -> None:
    print(f"PDF: {PDF}")

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_picture_images = True
    # images_scale defaults to 1.0 (72 DPI). Bump for sharper output.
    pipeline_options.images_scale = 2.0

    print("Building DocumentConverter with picture extraction enabled ...")
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )

    print("Converting (this may take a while for large PDFs) ...")
    import time

    t0 = time.time()
    # page_range is an inclusive 1-based slice; max_num_pages is a REJECT
    # threshold (docs larger than N are marked invalid). For "only look at
    # the first N pages" we want page_range, not max_num_pages.
    result = converter.convert(PDF, page_range=(1, 10))
    elapsed = time.time() - t0
    print(f"Conversion took {elapsed:.1f}s (pages 1-10)")

    doc = result.document
    print(f"\nDocument summary:")
    print(f"  texts      : {len(doc.texts)}")
    print(f"  tables     : {len(doc.tables)}")
    print(f"  pictures   : {len(doc.pictures)}")
    print(f"  pages      : {len(doc.pages)}")

    # Full attribute dump for the first PictureItem to see what's available
    if doc.pictures:
        first = doc.pictures[0]
        print(f"\nFirst picture ({type(first).__name__}) attributes:")
        for attr in sorted(dir(first)):
            if attr.startswith("_"):
                continue
            try:
                val = getattr(first, attr)
                if callable(val):
                    print(f"  method: {attr}(...)")
                else:
                    repr_val = repr(val)
                    if len(repr_val) > 120:
                        repr_val = repr_val[:120] + "..."
                    print(f"  {attr}: {repr_val}")
            except Exception as e:
                print(f"  {attr}: <error: {e}>")

        # Try get_image
        print(f"\nTrying first.get_image(doc) ...")
        try:
            pil = first.get_image(doc)
            print(f"  get_image returned: {type(pil).__name__}")
            if pil is not None:
                print(f"  size: {pil.size}")
                print(f"  mode: {pil.mode}")
        except Exception as e:
            print(f"  get_image FAILED: {type(e).__name__}: {e}")

        # Try .image (direct attribute access)
        img_attr = getattr(first, "image", None)
        print(f"\nfirst.image attribute: {type(img_attr).__name__ if img_attr else 'None'}")
        if img_attr is not None:
            print(f"  attributes of first.image:")
            for a in sorted(dir(img_attr)):
                if a.startswith("_"):
                    continue
                try:
                    v = getattr(img_attr, a)
                    if callable(v):
                        continue
                    repr_v = repr(v)
                    if len(repr_v) > 80:
                        repr_v = repr_v[:80] + "..."
                    print(f"    {a}: {repr_v}")
                except Exception:
                    pass

        # Provenance
        prov = getattr(first, "prov", None)
        print(f"\nProv: {prov}")
        if prov:
            for i, p in enumerate(prov):
                print(f"  prov[{i}] type: {type(p).__name__}")
                for pa in sorted(dir(p)):
                    if pa.startswith("_"):
                        continue
                    try:
                        pv = getattr(p, pa)
                        if callable(pv):
                            continue
                        print(f"    {pa}: {pv!r}")
                    except Exception:
                        pass

        # Caption
        if hasattr(first, "caption_text"):
            try:
                cap = first.caption_text(doc)
                print(f"\ncaption_text: {cap!r}")
            except Exception as e:
                print(f"\ncaption_text FAILED: {e}")

    # Enumerate up to MAX_PICTURES
    print(f"\n--- First {min(MAX_PICTURES, len(doc.pictures))} pictures ---")
    for i, pic in enumerate(doc.pictures[:MAX_PICTURES]):
        print(f"\n[{i}] {type(pic).__name__}")
        try:
            pil = pic.get_image(doc)
            if pil is not None:
                print(f"    size={pil.size}  mode={pil.mode}")
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                print(f"    png bytes: {len(buf.getvalue()):,}")
            else:
                print(f"    get_image returned None")
        except Exception as e:
            print(f"    get_image FAILED: {type(e).__name__}: {e}")
        prov = getattr(pic, "prov", None)
        if prov:
            for p in prov:
                page = getattr(p, "page_no", None)
                bbox = getattr(p, "bbox", None)
                print(f"    page={page}  bbox={bbox}")


if __name__ == "__main__":
    main()
