import { Link } from "react-router-dom";
import { Layout } from "../components/Layout.js";

export function SettingsPage() {
  return (
    <Layout>
      <div className="SettingsPage">
        <h1>Settings</h1>
        <p>Settings will appear here.</p>
        <Link to="/" className="SettingsPage-back">
          &larr; Back to REPL
        </Link>
      </div>
    </Layout>
  );
}
