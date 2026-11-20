import React, { Suspense, lazy } from 'react';
import { Navigate, Routes, Route, useLocation } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';

// [Phúc] Phân hệ Chủ quán (Owner Portal — Lazy Loaded)
const PublicOwnerRegisterPage = lazy(() => import('./features/owner-portal/pages/PublicOwnerRegisterPage.jsx'));
const OwnerLayout = lazy(() => import('./features/owner-portal/OwnerLayout.jsx'));

// [Vũ] Phân hệ Quản trị (Admin Dashboard — Lazy Loaded)
const LoginPage = lazy(() => import('./features/admin-dashboard/pages/LoginPage.jsx'));
const AdminLayout = lazy(() => import('./features/admin-dashboard/AdminLayout.jsx'));

/**
 * App Router cho website Chủ quán và Quản trị:
 * - `/admin/login`     -> Trang đăng nhập chung (GuestRoute)
 * - `/register/owner`  -> Trang đăng ký chủ quán công khai (Phúc)
 * - `/admin/*`         -> Admin Dashboard (Vũ)
 * - `/owner/*`         -> Owner Portal B2B (Phúc)
 */
export default function App() {
  const location = useLocation();
  return (
    <Suspense fallback={<div className="p-6 text-center">Đang tải phân hệ...</div>}>
      {location.pathname.startsWith('/admin') && <Toaster position="top-right"
        containerStyle={{ zIndex: 10000 }}
        toastOptions={{ duration: 5000, style: { maxWidth: 440 }, error: { duration: 7000 } }} />}
      <Routes>
        <Route path="/admin/login" element={<LoginPage />} />
        <Route path="/register/owner" element={<PublicOwnerRegisterPage />} />
        <Route path="/admin/*" element={<AdminLayout />} />
        <Route path="/owner/*" element={<OwnerLayout />} />
        <Route path="*" element={<Navigate to="/admin/login" replace />} />
      </Routes>
    </Suspense>
  );
}
