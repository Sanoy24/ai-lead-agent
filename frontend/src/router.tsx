import { createBrowserRouter } from "react-router";

import { Campaigns } from "@/pages/admin/Campaigns";
import { AdminLayout } from "@/pages/admin/Layout";
import { LeadDetail } from "@/pages/admin/LeadDetail";
import { Leads } from "@/pages/admin/Leads";
import { Login } from "@/pages/admin/Login";
import { Overview } from "@/pages/admin/Overview";
import { Landing } from "@/pages/Landing";
import { NotFound } from "@/pages/NotFound";

export const router = createBrowserRouter([
  { path: "/", element: <Landing /> },
  { path: "/admin/login", element: <Login /> },
  {
    path: "/admin",
    element: <AdminLayout />,
    children: [
      { index: true, element: <Overview /> },
      { path: "leads", element: <Leads /> },
      { path: "leads/:id", element: <LeadDetail /> },
      { path: "campaigns", element: <Campaigns /> },
    ],
  },
  { path: "*", element: <NotFound /> },
]);
