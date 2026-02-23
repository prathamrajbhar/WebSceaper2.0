async function apiCall(endpoint, data) {
    const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'API request failed');
    }
    return await response.json();
}

function displayResults(data, areaId) {
    const area = document.getElementById(areaId);
    area.innerHTML = '';

    if (areaId === 'search-results') {
        if (!data.organic_results || data.organic_results.length === 0) {
            area.innerHTML = '<div class="text-muted">No results found.</div>';
            return;
        }

        data.organic_results.forEach(res => {
            const div = document.createElement('div');
            div.className = 'result-item animate-fade';
            div.innerHTML = `
                <a href="${res.link}" target="_blank" class="result-title">${res.title}</a>
                <span class="result-link">${res.link}</span>
                <p class="result-snippet">${res.snippet}</p>
            `;
            area.appendChild(div);
        });
    } else {
        // Scrape results
        area.innerHTML = `
            <div class="result-item animate-fade">
                <h3 class="result-title">${data.content.title}</h3>
                <p class="result-snippet">Word Count: ${data.content.word_count}</p>
                <div class="json-view" style="margin-top: 1rem; font-size: 0.8rem; white-space: pre-wrap;">
                    ${data.content.content.join('\n\n')}
                </div>
            </div>
        `;
    }
}

async function handleSearch() {
    const query = document.getElementById('search-query').value;
    const engine = document.getElementById('search-engine').value;
    const btn = document.getElementById('search-btn');

    if (!query) return;

    btn.disabled = true;
    btn.innerHTML = 'Searching...';

    try {
        const results = await apiCall('/api/search', { query, engine });
        displayResults(results, 'search-results');
    } catch (err) {
        alert(err.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = 'Search';
    }
}

async function handleScrape() {
    const url = document.getElementById('scrape-url').value;
    const btn = document.getElementById('scrape-btn');

    if (!url) return;

    btn.disabled = true;
    btn.innerHTML = 'Scraping...';

    try {
        const results = await apiCall('/api/scrape', { url });
        displayResults(results, 'scrape-results');
    } catch (err) {
        alert(err.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = 'Scrape';
    }
}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        const target = tab.dataset.target;

        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');

        document.querySelectorAll('.card').forEach(card => {
            if (card.id === target + '-card') {
                card.style.display = 'block';
            } else if (card.id !== 'docs-card') {
                card.style.display = 'none';
            }
        });
    });
});
