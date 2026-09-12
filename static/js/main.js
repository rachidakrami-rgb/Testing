// ===== JavaScript الرئيسي للعيادة =====
document.addEventListener('DOMContentLoaded', function () {

  // ===== البحث السريع =====
  const searchInput = document.getElementById('globalSearch');
  const searchResults = document.getElementById('searchResults');
  if (searchInput && searchResults) {
    let timer;
    searchInput.addEventListener('input', function () {
      clearTimeout(timer);
      const q = this.value.trim();
      if (q.length < 1) { searchResults.style.display = 'none'; return; }
      timer = setTimeout(() => {
        fetch(`/api/search?q=${encodeURIComponent(q)}`)
          .then(r => r.json())
          .then(data => {
            searchResults.innerHTML = '';
            if (data.length === 0) {
              searchResults.innerHTML = '<div style="padding:12px;color:#888;text-align:center">لا توجد نتائج</div>';
            } else {
              data.forEach(p => {
                const a = document.createElement('a');
                a.href = `/patients/${p.id}`;
                a.innerHTML = `<span><strong>${p.code}</strong> - ${p.name}</span><small>${p.phone}</small>`;
                searchResults.appendChild(a);
              });
            }
            searchResults.style.display = 'block';
          });
      }, 300);
    });
    document.addEventListener('click', e => {
      if (!searchInput.contains(e.target) && !searchResults.contains(e.target))
        searchResults.style.display = 'none';
    });
  }

  // ===== قائمة الجوال (الشريط الزجاجي) =====
  const menuToggle = document.getElementById('menuToggle');
  const navLinks = document.getElementById('navLinks');
  if (menuToggle && navLinks) {
    menuToggle.addEventListener('click', () => navLinks.classList.toggle('open'));
    // إغلاق القائمة عند اختيار رابط أو النقر خارجها
    navLinks.querySelectorAll('a').forEach(a =>
      a.addEventListener('click', () => navLinks.classList.remove('open')));
    document.addEventListener('click', e => {
      if (!menuToggle.contains(e.target) && !navLinks.contains(e.target))
        navLinks.classList.remove('open');
    });
  }

  // ===== تأكيد الحذف =====
  document.querySelectorAll('.btn-delete').forEach(btn => {
    btn.addEventListener('click', e => {
      if (!confirm('هل أنت متأكد من الحذف؟ لا يمكن التراجع عن هذا الإجراء.')) {
        e.preventDefault();
      }
    });
  });

  // ===== ملء رسوم الزيارة الافتراضية =====
  const feeField = document.getElementById('fee');
  if (feeField && feeField.value === '' && window.DEFAULT_FEE) {
    feeField.value = window.DEFAULT_FEE;
  }
});