import { Route, Routes } from "react-router-dom";
import { ReplPage } from "./pages/ReplPage.js";
import { SettingsPage } from "./pages/SettingsPage.js";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<ReplPage />} />
      <Route path="/settings" element={<SettingsPage />} />
    </Routes>
  );
}
