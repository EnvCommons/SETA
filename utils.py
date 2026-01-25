import re
import tempfile
from pathlib import Path
from typing import Optional

from openreward import SandboxesAPI


async def upload_text(
    sandbox: SandboxesAPI,
    remote_path: str,
    content: str,
    *,
    ensure_trailing_newline: bool = False,
) -> None:
    """Upload text content to a file on the remote computer."""
    data = content if not ensure_trailing_newline or content.endswith("\n") else f"{content}\n"

    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(data)
        tmp.flush()
        temp_path = Path(tmp.name)

    try:
        await sandbox.upload(str(temp_path), remote_path)
    finally:
        temp_path.unlink(missing_ok=True)


async def download_text(
    sandbox: SandboxesAPI,
    remote_path: str,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str:
    """Download text content from the remote computer."""
    file_bytes = await sandbox.download(remote_path)
    return file_bytes.decode(encoding, errors)

def extract_flag_from_readme(readme_path: str) -> Optional[str]:
    """Extract flag from README.md file."""
    try:
        with open(readme_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # Common flag patterns - same as in test_extract.py
        flag_patterns = [
            r'flag:\s*`([^`]+)`',
            r'flag:\s*([A-Za-z0-9_{}\[\]@!#$%^&*(),.?":;|<>~+-]+)',
            r'Flag:\s*`([^`]+)`',
            r'Flag:\s*([A-Za-z0-9_{}\[\]@!#$%^&*(),.?":;|<>~+-]+)',
            r'(flag\{[^}]+\})',
            r'(RITSEC\{[^}]+\})',
            r'(CTF\{[^}]+\})',
            r'(secarmy\{[^}]+\})',
            r'(paseca\{[^}]+\})',
            r'(rooters\{[^}]+\})',
            r'(picoCTF\{[^}]+\})',
            r'(PICO\{[^}]+\})',
            r'(uiuctf\{[^}]+\})',
            r'(hsctf\{[^}]+\})',
            r'(utflag\{[^}]+\})',
            r'(csaw\{[^}]+\})',
            r'(nactf\{[^}]+\})',
            r'(tjctf\{[^}]+\})',
            r'(actf\{[^}]+\})',
            r'(ictf\{[^}]+\})',
            r'(TUCTF\{[^}]+\})',
            r'(SECT\{[^}]+\})',
            r'(TWCTF\{[^}]+\})',
            r'(SECCON\{[^}]+\})',
            r'(RS\{[^}]+\})',
            r'(KAF\{[^}]+\})',
            r'(KorNewbie\{[^}]+\})',
            r'(watevr\{[^}]+\})',
            r'(X-MAS\{[^}]+\})',
            r'(AFFCTF\{[^}]+\})',
            r'(d4rk\{[^}]+\})',
            r'(justCTF\{[^}]+\})',
            r'(utc\{[^}]+\})',
            # And many more CTF formats...
        ]
        
        # Try each pattern
        for pattern in flag_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            if matches:
                # Return the first match, handling both group and non-group patterns
                flag = matches[0]
                if isinstance(flag, tuple):
                    flag = flag[0]
                return flag.strip()
        
        return None
        
    except Exception as e:
        print(f"Error reading {readme_path}: {e}")
        return None


def extract_prompt_from_readme(readme_path: str) -> str:
    """Extract the challenge prompt from README.md file."""
    try:
        with open(readme_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # Look for content in > quotation blocks
        quote_pattern = r'> (.+?)(?=\n\n|\n#|\n>|\n\[|\n```|\nAuthor|\nflag:|\Z)'
        matches = re.findall(quote_pattern, content, re.DOTALL | re.IGNORECASE)
        
        if matches:
            # Join all quote blocks and clean up
            prompt = '\n'.join(matches).strip()
            # Remove HTML tags
            prompt = re.sub(r'<[^>]+>', '', prompt)
            # Remove extra whitespace
            prompt = re.sub(r'\s+', ' ', prompt).strip()
            return prompt
        
        # If no quotes found, return the first paragraph after the title
        lines = content.split('\n')
        in_content = False
        content_lines = []
        
        for line in lines:
            if line.startswith('# '):
                in_content = True
                continue
            elif in_content and line.startswith('#'):
                break
            elif in_content and line.strip() and not line.startswith('['):
                content_lines.append(line.strip())
                if len(content_lines) >= 3:  # Limit to first few lines
                    break
        
        if content_lines:
            return ' '.join(content_lines)
        
        return "No prompt found"
        
    except Exception as e:
        print(f"Error reading {readme_path}: {e}")
        return "Error reading prompt"
