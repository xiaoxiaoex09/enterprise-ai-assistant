"""Generate the original, redistributable demonstration manual.

Run from the repository root: python demo/create_demo_pdf.py
"""

from pathlib import Path

import pymupdf


OUTPUT = Path(__file__).resolve().parent / "assets" / "asteredge_x100_guide.pdf"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        lines = [
            ("AsterEdge X100 - Demonstration Field Guide", 18),
            ("Original fictional sample for this open-source portfolio project.", 10),
            ("Model: X100   Version: 1.0   Document type: service guide", 10),
            ("", 10),
            ("Fault code E-17: amber status LED flashes twice every five seconds.", 11),
            ("Meaning: the sensor is not receiving a stable 12 V DC supply.", 11),
            ("Check 1: disconnect power before touching the terminal block.", 11),
            ("Check 2: verify the supply at the X100 input is 11.5 to 12.5 V DC.", 11),
            ("Check 3: inspect the power cable and terminal screws for looseness.", 11),
            ("Check 4: restore power and wait 30 seconds before checking the LED.", 11),
            ("If E-17 remains, record the measured voltage and escalate to support.", 11),
            ("", 10),
            ("Safety: this fictional guide is a software demo, not real repair advice.", 10),
        ]
        y = 70
        for line, size in lines:
            if line:
                page.insert_text((55, y), line, fontsize=size, fontname="helv")
            y += 42 if size == 18 else 27
        document.save(OUTPUT, garbage=4, deflate=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
