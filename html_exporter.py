import json
import os

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MagicFit AI, Creator DM Tool</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#09090b;--card:#111113;--card-hover:#18181b;--border:#27272a;--accent:#8b5cf6;--accent2:#a78bfa;--green:#22c55e;--red:#ef4444;--orange:#f59e0b;--blue:#3b82f6;--pink:#ec4899;--text:#fafafa;--text2:#a1a1aa;--text3:#71717a}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}
.top-bar{background:linear-gradient(180deg,#111113 0%,#09090b 100%);padding:20px;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;backdrop-filter:blur(12px); display:flex; justify-content:space-between; align-items:center;}
.top-bar-left h1{font-size:18px;font-weight:800;letter-spacing:-.3px}
.top-bar-left p{font-size:12px;color:var(--text3);margin-top:2px}
.top-bar-right {display:flex; gap: 10px;}
.stats-row{display:flex;gap:8px;margin-top:14px;overflow-x:auto;padding-bottom:4px;-ms-overflow-style:none;scrollbar-width:none}
.stats-row::-webkit-scrollbar{display:none}
.stat-pill{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:8px 14px;white-space:nowrap;min-width:fit-content}
.stat-pill .num{font-size:18px;font-weight:800;color:var(--text);display:block;line-height:1}
.stat-pill .lbl{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.stat-pill.sent{border-color:var(--green)}
.stat-pill.sent .num{color:var(--green)}
.controls{padding:14px 20px;background:var(--bg);position:sticky;top:88px;z-index:99;border-bottom:1px solid var(--border)}
.search-wrap{position:relative}
.search-wrap svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--text3)}
.search{width:100%;padding:10px 12px 10px 38px;background:var(--card);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none;font-family:inherit}
.search:focus{border-color:var(--accent)}
.search::placeholder{color:var(--text3)}
.chips{display:flex;gap:6px;margin-top:10px;overflow-x:auto;padding-bottom:4px;-ms-overflow-style:none;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{padding:6px 14px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer;transition:.15s;white-space:nowrap;font-family:inherit;font-weight:500}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.chip:active{transform:scale(.96)}
.list{padding:10px 20px 100px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;margin-bottom:10px;overflow:hidden;transition:.15s}
.card.done{opacity:.5}
.card.done .card-top{background:rgba(34,197,94,.04)}
.card-top{display:flex;align-items:center;padding:14px;gap:12px;cursor:pointer;-webkit-tap-highlight-color:transparent;user-select:none}
.chk{width:22px;height:22px;border-radius:6px;border:2px solid var(--border);display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:.15s;cursor:pointer}
.chk.checked{background:var(--green);border-color:var(--green)}
.chk.checked::after{content:"✓";color:#fff;font-size:13px;font-weight:700}
.card-info{flex:1;min-width:0}
.card-handle{font-weight:700;font-size:14px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-name{font-size:11px;color:var(--text3);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-badges{display:flex;gap:5px;flex-shrink:0}
.badge{font-size:9px;padding:3px 7px;border-radius:6px;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
.badge.new{background:rgba(239,68,68,.12);color:var(--red);border:1px solid rgba(239,68,68,.25)}
.badge.old{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2)}
.badge.t1{background:rgba(245,158,11,.1);color:var(--orange);border:1px solid rgba(245,158,11,.2)}
.badge.t2{background:rgba(59,130,246,.1);color:var(--blue);border:1px solid rgba(59,130,246,.2)}
.badge.t3{background:rgba(113,113,122,.1);color:var(--text3);border:1px solid rgba(113,113,122,.2)}
.arrow-ico{color:var(--text3);transition:.2s;font-size:10px;flex-shrink:0}
.card.open .arrow-ico{transform:rotate(180deg)}
.card-body{display:none;padding:0 14px 14px;border-top:1px solid var(--border)}
.card.open .card-body{display:block}
.variant-tag{font-size:10px;color:var(--accent2);margin-top:10px;font-weight:600;letter-spacing:.3px}
.dm-box{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:8px;font-size:13px;line-height:1.7;color:#d4d4d8;white-space:pre-wrap;max-height:280px;overflow-y:auto}
.action-btn{width:100%;margin-top:12px;padding:14px;border-radius:10px;border:none;font-size:14px;font-weight:700;cursor:pointer;transition:.15s;font-family:inherit;display:flex;align-items:center;justify-content:center;gap:8px;background:linear-gradient(135deg,var(--accent) 0%,var(--pink) 100%);color:#fff}
.action-btn:active{transform:scale(.97)}
.action-btn.copied{background:linear-gradient(135deg,var(--green) 0%,#16a34a 100%)}
.download-btn {padding: 8px 16px; border-radius: 8px; border: none; background: var(--green); color: white; font-weight: bold; font-family: inherit; font-size: 12px; cursor: pointer;}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--green);color:#fff;padding:12px 28px;border-radius:12px;font-size:13px;font-weight:700;opacity:0;transition:.25s;z-index:200;pointer-events:none;box-shadow:0 8px 30px rgba(34,197,94,.3)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.empty{text-align:center;padding:60px 20px;color:var(--text3);font-size:14px}
</style>
</head>
<body>
<div class="top-bar">
    <div class="top-bar-left">
        <h1>MagicFit AI, DM Outreach</h1>
        <p>Tap card → Copy DM & open IG profile in one click</p>
        <div class="stats-row" id="stats"></div>
    </div>
    <div class="top-bar-right">
        <button class="download-btn" onclick="downloadProgress()">Download Progress</button>
    </div>
</div>
<div class="controls">
<div class="search-wrap">
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
<input type="text" class="search" id="search" placeholder="Search handle, name, or keyword...">
</div>
<div class="chips" id="chips">
<button class="chip on" data-f="all">All</button>
<button class="chip" data-f="new">🆕 New</button>
<button class="chip" data-f="existing">✅ Existing</button>
<button class="chip" data-f="t1">🔥 100K+</button>
<button class="chip" data-f="t2">⚡ 50-100K</button>
<button class="chip" data-f="t3">📌 &lt;50K</button>
<button class="chip" data-f="unsent">📩 Not Yet DMed</button>
<button class="chip" data-f="sent">✅ Already DMed</button>
</div>
</div>
<div class="list" id="list"></div>
<div class="toast" id="toast">✅ DM copied & profile opening...</div>

<script>
const V=[
(h)=>`hey 👋 been meaning to reach out for a while.\n\n${h}\n\nim with @magicfitai, we turn product urls into ugc ads instantly. putting together a small group of creators for a paid collab (upfront + 12 months comms).\n\nthought u'd be a great fit, open to hearing more?`,
(h)=>`yo! quick one,\n\n${h}\n\nwere running a paid creator campaign at @magicfitai this month. upfront payment + monthly commissions for a full year.\n\nworth a convo?`,
(h)=>`hey! random q, have u come across @magicfitai yet?\n\n${h}\n\nwe turn any product url into a full ugc ad creative with ai. looking for a handful of creators for a paid partnership, upfront fee plus ongoing comms.\n\ncan i send details?`,
(h)=>`hey, had to reach out, ${h}\n\ni run creator partnerships at @magicfitai and ur content is exactly the kind of thing that resonates. were doing a paid collab with upfront payment and 12 months of comms built in.\n\nwould love to share more if ur open!`,
(h)=>`hey! we just wrapped collabs with a bunch of creators in the ai/tech space and the results have been 🔥\n\n${h}\n\nweve got a few more spots open for @magicfitai, its a paid campaign. ur audience would be a perfect match.\n\nshould i send the details?`,
(h)=>`hey, no pitch spam i promise 😅\n\n${h}\n\nim rajdeep from @magicfitai. we're doing a small creator campaign, paid upfront with comms that run for 12 months after ur post. totally understand if the timings off, but if ur curious id love to share the details.\n\nlmk!`
];
const VNAMES=["Casual Fan","Short & Punchy","Question Opener","Compliment First","Social Proof","No-Pressure Genuine"];

const C=__CREATORS_JSON__;

let sent = JSON.parse(localStorage.getItem('mf_sent')||'[]');
let filter = 'all';
let q = '';

function updateStats(){
    const t = C.length, s = sent.length;
    document.getElementById('stats').innerHTML = `
        <div class="stat-pill"><span class="num">${t}</span><span class="lbl">Total</span></div>
        <div class="stat-pill sent"><span class="num">${s}</span><span class="lbl">Sent (${Math.round(s/t*100||0)}%)</span></div>
        <div class="stat-pill"><span class="num">${t-s}</span><span class="lbl">Remaining</span></div>
    `;
}

function render(){
    updateStats();
    const list = document.getElementById('list');
    let html='';
    let c = 0;
    
    C.forEach((u, i) => {
        const isSent = sent.includes(u.h);
        if(filter === 'new' && u.s !== 'new') return;
        if(filter === 'existing' && u.s !== 'existing') return;
        if(filter === 't1' && u.t !== 't1') return;
        if(filter === 't2' && u.t !== 't2') return;
        if(filter === 't3' && u.t !== 't3') return;
        if(filter === 'sent' && !isSent) return;
        if(filter === 'unsent' && isSent) return;
        
        if(q){
            const str = (u.h+u.n+u.hook).toLowerCase();
            if(!str.includes(q)) return;
        }
        
        c++;
        
        const bStat = u.s==='new'? '<span class="badge new">🆕 New</span>':'<span class="badge old">✅ Exst</span>';
        let bTier = '';
        if(u.t==='t1') bTier = '<span class="badge t1">🔥 100K+</span>';
        if(u.t==='t2') bTier = '<span class="badge t2">⚡ 50-100K</span>';
        if(u.t==='t3') bTier = '<span class="badge t3">📌 &lt;50K</span>';
        
        let bodyHtml = '';
        V.forEach((tmpl, vi)=>{
            const text = tmpl(u.hook);
            bodyHtml += `
                <div class="variant-tag">Variant ${vi+1}: ${VNAMES[vi]}</div>
                <div class="dm-box" id="dm_${i}_${vi}">${text}</div>
                <button class="action-btn" onclick="copyAndOpen('${i}_${vi}', '${u.h}', event)">
                    Copy & Open Instagram
                </button>
            `;
        });
        
        html += `
            <div class="card ${isSent?'done':''}" id="c_${i}">
                <div class="card-top" onclick="toggle(${i}, event)">
                    <div class="chk ${isSent?'checked':''}" onclick="toggleCheck(${i}, '${u.h}', event)"></div>
                    <div class="card-info">
                        <div class="card-handle">${u.h}</div>
                        <div class="card-name">${u.n}</div>
                    </div>
                    <div class="card-badges">${bStat}${bTier}</div>
                    <div class="arrow-ico">▼</div>
                </div>
                <div class="card-body">
                    ${bodyHtml}
                </div>
            </div>
        `;
    });
    
    if(c===0) html='<div class="empty">No creators match your filters.</div>';
    list.innerHTML = html;
}

function toggle(i, e){
    if(e.target.closest('.chk')) return;
    document.getElementById('c_'+i).classList.toggle('open');
}

function toggleCheck(i, h, e){
    if(e) e.stopPropagation();
    const idx = sent.indexOf(h);
    if(idx===-1) sent.push(h);
    else sent.splice(idx,1);
    localStorage.setItem('mf_sent', JSON.stringify(sent));
    render();
}

function copyAndOpen(id, h, e){
    e.stopPropagation();
    const text = document.getElementById('dm_'+id).innerText;
    navigator.clipboard.writeText(text).then(()=>{
        const btn = e.target;
        btn.innerText = 'Copied! Opening IG...';
        btn.classList.add('copied');
        
        const toast = document.getElementById('toast');
        toast.classList.add('show');
        setTimeout(()=>toast.classList.remove('show'), 2000);
        
        setTimeout(()=>{
            window.open('https://instagram.com/'+h.replace('@',''), '_blank');
            btn.innerText = 'Copy & Open Instagram';
            btn.classList.remove('copied');
            
            // auto check
            if(!sent.includes(h)) toggleCheck(null, h, null);
        }, 600);
    });
}

function downloadProgress() {
    const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(sent));
    const dlAnchorElem = document.createElement('a');
    dlAnchorElem.setAttribute("href", dataStr);
    dlAnchorElem.setAttribute("download", "dm_progress.json");
    dlAnchorElem.click();
}

document.getElementById('search').addEventListener('input', (e)=>{
    q = e.target.value.toLowerCase();
    render();
});

document.querySelectorAll('.chip').forEach(c=>{
    c.addEventListener('click', (e)=>{
        document.querySelectorAll('.chip').forEach(ch=>ch.classList.remove('on'));
        e.target.classList.add('on');
        filter = e.target.getAttribute('data-f');
        render();
    });
});

render();
</script>
</body>
</html>
"""

def generate_dm_tool(creators: list, output_path: str):
    """
    creators is a list of dicts:
    [ { "h": "@handle", "n": "Full Name", "t": "t1", "s": "new", "hook": "The LLM personalized hook" } ]
    """
    json_str = json.dumps(creators)
    html_content = HTML_TEMPLATE.replace("__CREATORS_JSON__", json_str)
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    return output_path
