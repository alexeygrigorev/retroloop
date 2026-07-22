// The one React island in the application, and the whole of it.
//
// What it proves, and all it proves, is that the pipeline works: state crossed
// from Django into React through the `json_script` block, and React is live
// rather than a rendered string, because a button inside it changes the screen
// with no server involved.
//
// It makes no request. There is no network call of any kind, no poll loop and
// no timer here on purpose: connecting the board to the network is #14's job,
// and it reads through the state endpoint from #11 and writes through the
// mutation endpoints from #12 — one channel, never a second invented here.
//
// #14 replaces this component outright.
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";

/** The element the board occupies. Empty in the page source, filled here. */
const MOUNT_ID = "retro-board";

/** The `json_script` block the Django template renders the bootstrap into. */
const BOOTSTRAP_ID = "retro-bootstrap";

/**
 * Read the bootstrap out of the page.
 *
 * It carries the retrospective's id, its stage, its version and the viewer's
 * own cards — nothing else, and no other member's card text, which is the one
 * thing a placeholder on a real retrospective page must never leak.
 */
function readBootstrap() {
  const element = document.getElementById(BOOTSTRAP_ID);
  if (element === null) {
    throw new Error(`No #${BOOTSTRAP_ID} block on the page: nothing to mount with.`);
  }
  return JSON.parse(element.textContent);
}

function Board({ id, stage, version, cards }) {
  // Local state, owned by the component. Toggling it re-renders from memory.
  const [cardsShown, setCardsShown] = useState(true);

  return (
    <>
      <h2 className="section-heading">The board</h2>
      <ul className="list-rows">
        <li data-board-retro>Retrospective #{id}</li>
        <li data-board-stage>Stage: {stage}</li>
        <li data-board-version>Version: {version}</li>
      </ul>
      <p className="list-rows">
        <button
          type="button"
          className="btn-secondary"
          data-board-toggle
          onClick={() => setCardsShown((shown) => !shown)}
        >
          {cardsShown ? "Hide my cards" : "Show my cards"}
        </button>
      </p>
      {cardsShown && (
        <ul className="list-rows" data-board-cards>
          {cards.length === 0 && <li>You have written no cards this week.</li>}
          {cards.map((card) => (
            <li key={card.id}>
              {card.category}: {card.text}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

const mount = document.getElementById(MOUNT_ID);
if (mount === null) {
  throw new Error(`No #${MOUNT_ID} element on the page: the island has nowhere to mount.`);
}

createRoot(mount).render(
  <StrictMode>
    <Board {...readBootstrap()} />
  </StrictMode>,
);
