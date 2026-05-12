import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import Overview from "./pages/Overview.tsx";
import Bots from "./pages/Bots.tsx";
import BotDetail from "./pages/BotDetail.tsx";
import Strategies from "./pages/Strategies.tsx";
import StrategyDetail from "./pages/StrategyDetail.tsx";
import Markets from "./pages/Markets.tsx";
import Audit from "./pages/Audit.tsx";
import Failures from "./pages/Failures.tsx";

const navClass = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-2 rounded text-sm font-medium transition-colors ${
    isActive
      ? "bg-gray-900 text-white"
      : "text-gray-300 hover:bg-gray-700 hover:text-white"
  }`;

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-950 text-gray-100">
        <nav className="bg-gray-800 px-4 py-2 flex gap-2 flex-wrap items-center">
          <span className="font-bold text-white mr-4">BidderBot</span>
          <NavLink to="/" end className={navClass}>Overview</NavLink>
          <NavLink to="/bots" className={navClass}>Bots</NavLink>
          <NavLink to="/strategies" className={navClass}>Strategies</NavLink>
          <NavLink to="/markets" className={navClass}>Markets</NavLink>
          <NavLink to="/audit" className={navClass}>Audit</NavLink>
          <NavLink to="/failures" className={navClass}>Failures</NavLink>
        </nav>
        <main className="p-4">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/bots" element={<Bots />} />
            <Route path="/bots/:botId" element={<BotDetail />} />
            <Route path="/strategies" element={<Strategies />} />
            <Route path="/strategies/:name" element={<StrategyDetail />} />
            <Route path="/markets" element={<Markets />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="/failures" element={<Failures />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
