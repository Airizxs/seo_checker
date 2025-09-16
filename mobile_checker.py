import requests
from bs4 import BeautifulSoup

# Use browser-like headers to reduce 403s during checks
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def check_mobile_responsiveness(url: str) -> dict:
    """
    A basic check for mobile responsiveness by looking for the viewport meta tag.
    A more advanced check would require a headless browser or an API.

    Args:
        url (str): The URL of the website to check.

    Returns:
        dict: A dictionary with the results of the check.
    """
    results = {
        'viewport_meta_tag': {'found': False, 'content': None},
        'status': 'fail'
    }

    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
    except requests.exceptions.RequestException as e:
        return {'status': 'error', 'message': f"Failed to access the URL for mobile check: {e}"}

    # Look for the viewport meta tag
    viewport_tag = soup.find('meta', attrs={'name': 'viewport'})
    
    if viewport_tag and 'content' in viewport_tag.attrs:
        results['viewport_meta_tag']['found'] = True
        results['viewport_meta_tag']['content'] = viewport_tag['content']
        
        # Simple check for common viewport settings
        if 'width=device-width' in viewport_tag['content'] and 'initial-scale=1' in viewport_tag['content']:
            results['status'] = 'pass'
            results['message'] = 'Viewport meta tag with common settings found.'
        else:
            results['status'] = 'warning'
            results['message'] = 'Viewport tag found, but content is not a standard mobile configuration.'
    else:
        results['status'] = 'fail'
        results['message'] = 'No viewport meta tag found, which is essential for mobile responsiveness.'

    return results
