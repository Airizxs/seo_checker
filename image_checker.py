import os
import re
from typing import Dict, List
from bs4 import BeautifulSoup


def check_images(soup: BeautifulSoup) -> Dict:
    images = soup.find_all('img')
    missing: List[str] = []
    poor: List[str] = []
    default_name_re = re.compile(r"^(img[_-]?\d+|dsc[_-]?\d+|image[_-]?\d+|photo[_-]?\d+)$", re.I)
    for img in images:
        alt = img.get('alt')
        alt_text = (str(alt).strip() if isinstance(alt, str) else None)
        if not alt_text:
            src_val = img.get('src') or img.get('data-src') or img.get('srcset') or ''
            missing.append(src_val if isinstance(src_val, str) else str(src_val))
        else:
            # Heuristics for poor alt quality
            # - Very short (<3 words)
            # - Alt equals filename (without extension) or looks like a default camera name
            src = img.get('src') or ''
            base = os.path.splitext(os.path.basename(src.split('?')[0]))[0]
            if len(alt_text.split()) < 3:
                poor.append(src or alt_text)
            elif base and (alt_text.lower() == base.lower() or default_name_re.match(base or '')):
                poor.append(src or alt_text)

    status = "pass" if images and not missing and not poor else ("warning" if images else "fail")
    return {
        "total_images": len(images),
        "missing_alt": missing[:50],  # cap to avoid huge outputs
        "poor_alt": poor[:50],
        "status": status,
        "message": (
            (
                f"All {len(images)} images have descriptive alt text." if images and not missing and not poor
                else ("No images found." if not images else (
                    f"{len(missing)} image(s) missing alt." if missing else f"{len(poor)} image(s) with weak alt."
                ))
            )
        )
    }
