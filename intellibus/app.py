import sys
import asyncio
import time
import re
from typing import List, Tuple, Optional, Dict

from playwright.async_api import async_playwright
from flask import Flask, request, render_template, jsonify

# Global configuration (keep these)
CONCURRENT_LIMIT = 20
search_cache: Dict[str, str] = {}

# --- Core Logic Functions (Keep these mostly as is) ---
def is_subsequence(small: List[str], big: List[str]) -> bool:
    """Return True if all words in 'small' appear in 'big' in order."""
    it = iter(big)
    return all(word in it for word in it)

def normalize_text(text: str) -> str:
    """Normalize text: lowercase, remove extra whitespace, hyphens, commas, and Unicode whitespace."""
    text = text.replace('-', '').replace(',', '')
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    return text.lower()

async def wait_for_results_update(page) -> None:
    await page.wait_for_function(
        "document.querySelector('span.page-results') && document.querySelector('span.page-results').textContent.trim() !== ''",
        timeout=0
    )

async def binary_search_partial(term: str, page, base_url: str, cancel_event: asyncio.Event) -> Optional[str]:
    words = term.split()
    lo, hi = 1, len(words)
    best: Optional[str] = None
    while lo <= hi:
        if cancel_event.is_set():
            return None
        mid = (lo + hi) // 2
        prefix = " ".join(words[:mid])
        await page.goto(base_url, wait_until="networkidle", timeout=0)
        await page.wait_for_selector("div.main-search input.search-term", timeout=30000)
        await page.fill("div.main-search input.search-term", prefix)
        await page.press("div.main-search input.search-term", "Enter")
        try:
            await wait_for_results_update(page)
        except asyncio.TimeoutError:
            partial_content = ""
        else:
            partial_content = (await page.text_content("span.page-results")) or ""
        if partial_content and "Displaying" in partial_content:
            best = prefix
            lo = mid + 1
        else:
            hi = mid - 1
    return best

async def search_term(term: str, base_url: str, context, cancel_event: asyncio.Event, semaphore: asyncio.Semaphore) -> Tuple[str, str]:
    if cancel_event.is_set():
        return term, "Cancelled"
    if term in search_cache:
        return term, search_cache[term]
    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(base_url, wait_until="networkidle", timeout=0)
            await page.wait_for_selector("div.main-search input.search-term", timeout=30000)
            await page.fill("div.main-search input.search-term", term)
            await page.press("div.main-search input.search-term", "Enter")
            try:
                await wait_for_results_update(page)
            except asyncio.TimeoutError:
                content = ""
            else:
                content = (await page.text_content("span.page-results")) or ""

            initial_result_type = ""
            full_match_prefix = "Displaying search results for:"

            partial: Optional[str] = None

            if content and full_match_prefix in content:
                displayed_term_match = re.search(rf"{re.escape(full_match_prefix)}\s*\"(.+?)\"", content)
                if displayed_term_match:
                    displayed_term = displayed_term_match.group(1).strip()
                    if normalize_text(term) == normalize_text(displayed_term):
                        initial_result_type = "full_match_prefix"
                    else:
                        initial_result_type = "larger_description_prefix"
                else:
                    initial_result_type = "larger_description_prefix_fail"
            elif content and "Displaying" in content:
                initial_result_type = "larger_description_general"
            else:
                partial = await binary_search_partial(term, page, base_url, cancel_event)
                if partial:
                    await page.goto(base_url, wait_until="networkidle", timeout=0)
                    await page.wait_for_selector("div.main-search input.search-term", timeout=30000)
                    await page.fill("div.main-search input.search-term", partial)
                    await page.press("div.main-search input.search-term", "Enter")
                    try:
                        await wait_for_results_update(page)
                    except asyncio.TimeoutError:
                        pass

                    description_cells = await page.query_selector_all("td[data-column='description']")

                    found_in_template = False
                    template_text = ""
                    template_id = "Not found"

                    normalized_partial = normalize_text(partial)
                    partial_words = normalized_partial.split()

                    for cell in description_cells:
                        cell_text = (await cell.text_content()).strip()
                        normalized_cell = normalize_text(cell_text)
                        cell_words = normalized_cell.split()

                        if is_subsequence(partial_words, cell_words):
                            found_in_template = True
                            template_text = cell_text

                            parent_row = await cell.evaluate_handle("node => node.parentElement")
                            id_element = await parent_row.query_selector("a.view-record")
                            if id_element:
                                template_id = (await id_element.text_content()).strip()

                            break

                    if found_in_template:
                        initial_result_type = "template_match"
                        description_text = template_text
                        term_id_number = template_id
                    else:
                        initial_result_type = "partial"
                else:
                    initial_result_type = "no_match"

            description_text = "Not found"
            term_id_number = "Not found"
            is_deleted_description = False
            found_full_description_match = False
            found_in_description = False

            if initial_result_type != "template_match":
                if initial_result_type != "no_match":
                    view_record_link = await page.query_selector("a.view-record")
                    if view_record_link:
                        term_id_number = (await view_record_link.text_content()).strip()

                    description_cells = await page.query_selector_all("td[data-column='description']")

                    matched_cell_text = ""

                    for cell in description_cells:
                        cell_text = (await cell.text_content()).strip()
                        normalized_cell_text = normalize_text(cell_text)
                        normalized_term = normalize_text(term)

                        if normalized_term == normalized_cell_text:
                            found_full_description_match = True
                            found_in_description = True
                            parent_row = await cell.evaluate_handle("node => node.parentElement")
                            notes_element = await parent_row.query_selector("td[data-column='notes']")
                            if notes_element:
                                notes_text = (await notes_element.text_content()).strip()
                                if re.search(r"deleted", normalize_text(notes_text)):
                                    is_deleted_description = True
                                    matched_cell_text = cell_text
                                    break
                            matched_cell_text = cell_text
                            break

                        elif normalized_term in normalized_cell_text:
                            found_in_description = True
                            parent_row = await cell.evaluate_handle("node => node.parentElement")
                            notes_element = await parent_row.query_selector("td[data-column='notes']")
                            if notes_element:
                                notes_text = (await notes_element.text_content()).strip()
                                if re.search(r"deleted", normalize_text(notes_text)):
                                    is_deleted_description = True
                                    matched_cell_text = cell_text
                                    break
                            matched_cell_text = cell_text
                            if found_full_description_match:
                                break

                    if not found_in_description and initial_result_type == "partial" and partial:
                        normalized_partial_words = normalize_text(partial).split()
                        for cell in description_cells:
                            cell_text = (await cell.text_content()).strip()
                            normalized_cell_words = normalize_text(cell_text).split()
                            if is_subsequence(normalized_partial_words, normalized_cell_words):
                                found_in_description = True
                                description_text = cell_text
                                break

            if initial_result_type == "template_match":
                result = f"Apart of a larger description (Example - {description_text} - Term ID: {term_id_number})"
            elif is_deleted_description:
                result = f"Deleted description found (Term ID: {term_id_number})"
            elif found_full_description_match:
                result = f"Full match found (Term ID: {term_id_number})"
            elif found_in_description:
                description_element = await page.query_selector("td[data-column='description']")
                if description_element:
                    dt = await description_element.text_content()
                    if dt:
                        description_text = dt.strip()
                result = f"Apart of a larger description (Example - {description_text} - Term ID: {term_id_number})"
            elif initial_result_type == "partial" and partial:
                result = f"Full match not found, but partial match found: '{partial}' (Term ID: {term_id_number})"
            elif initial_result_type == "no_match":
                result = "No match found"
            elif view_record_link:
                description_element = await page.query_selector("td[data-column='description']")
                if description_element:
                    dt = await description_element.text_content()
                    if dt:
                        description_text = dt.strip()
                result = f"Apart of a larger description (Example - {description_text} - Term ID: {term_id_number})"
            else:
                result = "Apart of a larger description (Example - Description not found - Term ID: Not found)"

            search_cache[term] = result
            return term, result
        finally:
            await page.close()

# --- Flask Application ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key' # Important for security in real apps

# --- Custom Jinja2 Filter ---
@app.template_filter('regex_search')
def regex_search_filter(value, pattern):
    """
    Custom Jinja2 filter to perform regex search.
    Returns the match object if found, otherwise None.
    """
    if value is None:
        return None
    return re.search(pattern, value)

# --- Flask Route ---
@app.route('/', methods=['GET', 'POST'])
async def index():
    results = {}
    search_time = None
    error_message = None
    terms_input = ""
    if request.method == 'POST':
        terms_input = request.form.get('search_terms', '')
        terms = [term.strip() for term in terms_input.split(';') if term.strip()]
        if terms:
            start_time = time.time()
            cancel_event = asyncio.Event() # For cancellation, not fully implemented in web UI yet
            semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True) # Consider making headless configurable for debugging
                    #executable_path="/home/Kittycat/.cache/ms-playwright/chromium-1155/chrome-linux/chrome"
                    context = await browser.new_context()
                    tasks = [search_term(term, "https://idm-tmng.uspto.gov/id-master-list-public.html", context, cancel_event, semaphore) for term in terms]
                    search_results = await asyncio.gather(*tasks, return_exceptions=True) # Gather results, capturing exceptions

                    for term, result in search_results:
                        if isinstance(result, Exception):
                            error_message = str(result) # Capture first error, for demo simplicity
                            results[term] = "Error during search" # Indicate error in results
                        else:
                            results[term] = result

                    await context.close()
                    await browser.close()
                search_time = time.time() - start_time
            except Exception as e:
                error_message = str(e)

    return render_template('index.html', results=results, search_time=search_time, error_message=error_message, terms_input=terms_input)

if __name__ == '__main__':
    app.run(debug=True) # debug=True for development, remove for production