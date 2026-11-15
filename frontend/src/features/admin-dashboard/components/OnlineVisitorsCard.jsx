import React, { useEffect, useState } from 'react';
import { FiUsers } from 'react-icons/fi';
import axiosClient from '../../../api/axiosClient.js';

export default function OnlineVisitorsCard() {
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function refresh() {
      if (pending || document.visibilityState === 'hidden') return;
      pending = true;
      try {
        const { data } = await axiosClient.get('/admin/dashboard/online-visitors', { signal: controller.signal });
        if (!controller.signal.aborted) { setSnapshot(data); setError(false); }
      } catch {
        if (!controller.signal.aborted) setError(true);
      } finally { pending = false; }
    }
    refresh();
    const timer = setInterval(refresh, 10000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      controller.abort(); clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, []);
  return <section className="admin-metric-card" aria-label="Thiết bị truy cập mobile trong 30 phút gần nhất">
    <div className="admin-metric-head"><span>Thiết bị truy cập mobile · 30 phút</span><FiUsers size={22} aria-hidden="true" /></div>
    <div className="admin-metric-value" aria-live="polite">
      {error ? '—' : snapshot ? new Intl.NumberFormat('vi-VN').format(snapshot.online_count) : '···'}
    </div>
    <p className="text-sm text-slate-600">
      {error ? 'Tạm thời không cập nhật được số thiết bị truy cập.' : 'Thiết bị duy nhất có hoạt động trong 30 phút gần nhất · Tự cập nhật mỗi 10 giây.'}
    </p>
    <p className="mt-1 text-xs text-slate-500">Mỗi lần kết nối hoặc heartbeat gia hạn phiên thêm 30 phút. Ngắt kết nối vẫn được tính đến khi phiên hết hạn; cùng thiết bị kết nối lại chỉ tính một lần.</p>
  </section>;
}
