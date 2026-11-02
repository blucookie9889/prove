// The animated hero of Wiki Explorer (built with the Stageshow kit): http://localhost:5173/demo.html
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import WikiExplorer from "./wiki-explorer";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <WikiExplorer />
  </StrictMode>,
);
