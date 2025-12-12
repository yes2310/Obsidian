// 추가 대시보드 기능들

// 전역 변수
let allJobs = [];
let searchQuery = '';
let statusFilter = 'all';

// 검색 및 필터 기능
function filterJobs() {
  let filtered = allJobs;

  // 검색 필터
  if (searchQuery) {
    filtered = filtered.filter(job =>
      (job.filename || '').toLowerCase().includes(searchQuery.toLowerCase())
    );
  }

  // 상태 필터
  if (statusFilter !== 'all') {
    filtered = filtered.filter(job => job.status === statusFilter);
  }

  return filtered;
}

// 다크 모드 토글
function toggleDarkMode() {
  const root = document.documentElement;
  const isDark = root.classList.toggle('dark-mode');
  localStorage.setItem('darkMode', isDark);

  const btn = document.getElementById('dark-mode-toggle');
  btn.textContent = isDark ? '☀️' : '🌙';
}

// 작업 삭제
async function deleteJob(jobId) {
  if (!confirm('이 작업을 삭제하시겠습니까?')) return;

  try {
    const res = await fetch(`/jobs/${jobId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('삭제 실패');
    await fetchJobs();
  } catch (err) {
    alert('작업 삭제 실패: ' + err.message);
  }
}

// 노트 다운로드
async function downloadNote(jobId, filename) {
  try {
    const res = await fetch(`/note/${jobId}`);
    if (!res.ok) throw new Error('노트 로드 실패');
    const data = await res.json();

    const blob = new Blob([data.content], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${filename}.md`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert('노트 다운로드 실패: ' + err.message);
  }
}

// 노트 복사
async function copyNote(jobId) {
  try {
    const res = await fetch(`/note/${jobId}`);
    if (!res.ok) throw new Error('노트 로드 실패');
    const data = await res.json();

    await navigator.clipboard.writeText(data.content);
    alert('노트가 클립보드에 복사되었습니다!');
  } catch (err) {
    alert('노트 복사 실패: ' + err.message);
  }
}

// 키보드 단축키
document.addEventListener('keydown', (e) => {
  // Ctrl/Cmd + K: 검색에 포커스
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault();
    document.getElementById('search-input')?.focus();
  }

  // ESC: 검색 초기화
  if (e.key === 'Escape') {
    const searchInput = document.getElementById('search-input');
    if (searchInput && document.activeElement === searchInput) {
      searchInput.value = '';
      searchQuery = '';
      fetchJobs();
    }
  }

  // 숫자 키 1-4: 상태 필터
  if (e.key >= '1' && e.key <= '4' && !e.ctrlKey && !e.metaKey) {
    const filters = ['all', 'completed', 'running', 'failed'];
    const filterSelect = document.getElementById('status-filter');
    if (filterSelect && document.activeElement.tagName !== 'INPUT') {
      filterSelect.value = filters[parseInt(e.key) - 1];
      statusFilter = filterSelect.value;
      fetchJobs();
    }
  }
});

// 다크 모드 CSS
const darkModeStyles = `
<style id="dark-mode-styles">
  :root.dark-mode {
    --bg-primary: #0a0e1a;
    --bg-card: #141b2e;
    --bg-hover: #1a2332;
    --border-color: #1e2942;
    --text-primary: #e8edf4;
    --text-secondary: #94a3b8;
    --text-muted: #6a737d;
  }
</style>
`;

// 초기화
if (localStorage.getItem('darkMode') === 'true') {
  document.documentElement.classList.add('dark-mode');
  document.head.insertAdjacentHTML('beforeend', darkModeStyles);
}
