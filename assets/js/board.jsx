// The one React island in the application, and the whole of it.
//
// #13 mounted a placeholder that proved the pipeline worked and made no network
// call. #14 replaces it outright with the real cluster board: it reads the
// board through the state endpoint from #11 and writes through the seven
// mutation endpoints from #12, and it learns every one of those addresses from
// the bootstrap the template renders — so no path is written down here, and the
// island cannot invent, duplicate or drift from a URL the server owns.
//
// What the board must never show, and does not: who wrote a card, or whether a
// card was written anonymously (`_docs/decisions.md` item 10). The one
// person-fact it draws is `mine`, a boolean the server computes about the viewer
// themselves; the island renders it and decides nothing. A card is addressed by
// the `id` the payload gives it, which from #73 on is the opaque `public_id`
// (item 9) — never a primary key, and the island neither parses it nor orders by
// it. `position`, which the server already sorted the cards into, is the only
// order on the board.
import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

/** The element the board occupies. Empty in the page source, filled here. */
const MOUNT_ID = "retro-board";

/** The `json_script` block the Django template renders the bootstrap into. */
const BOOTSTRAP_ID = "retro-bootstrap";

/** How often an open board asks the server whether anything has changed. */
const POLL_INTERVAL = 1500;

/** The two stages in which the board's shape may be changed — the same window
 *  `projects/permissions.py` enforces server-side. Outside it, the controls are
 *  not offered, because the endpoints would refuse them anyway. */
const SHAPING_STAGES = new Set(["REVEAL", "CLUSTER"]);

const CATEGORY_LABELS = { START: "Start", STOP: "Stop", CONTINUE: "Continue" };

const MESSAGES = {
  offline: "Can't reach the server. The board is showing the last version it received.",
  refresh: "The board could not be refreshed just now. It will keep trying.",
  action: "That change could not be applied, so the board is unchanged.",
  gone: "This board is no longer available to you. Reload the page to see where the retrospective has got to.",
};

/**
 * Read the bootstrap out of the page: the retrospective's id, its stage and
 * version, the viewer's own cards, and the endpoint URLs the island talks to.
 */
function readBootstrap() {
  const element = document.getElementById(BOOTSTRAP_ID);
  if (element === null) {
    throw new Error(`No #${BOOTSTRAP_ID} block on the page: nothing to mount with.`);
  }
  return JSON.parse(element.textContent);
}

/** The CSRF token Django set as a cookie, for the POST writes. Read rather than
 *  rendered into a script, so no token sits in the page source. */
function readCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function categoryLabel(category) {
  return CATEGORY_LABELS[category] ?? category;
}

/** Group the cards into their columns without reordering them: the server sent
 *  them in `position` order, and that is the only order the board shows. */
function groupCards(cards) {
  const groups = new Map();
  groups.set(null, []);
  for (const card of cards) {
    const key = card.cluster ?? null;
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(card);
  }
  return groups;
}

function Card({ card, column, canShape, clusters, selected, actions }) {
  const inCluster = column.id !== null;
  return (
    <li
      className="board-card"
      draggable={canShape}
      onDragStart={canShape ? (event) => actions.dragStart(event, card.id) : undefined}
      onDragEnd={canShape ? actions.dragEnd : undefined}
    >
      <p className="board-card-head">
        <span className="board-tag">{categoryLabel(card.category)}</span>
        {card.mine && <span className="board-tag">Yours</span>}
      </p>
      <p className="board-card-text">{card.text}</p>
      {canShape && (
        <p className="board-actions">
          <label className="board-select">
            Move to
            <select
              value={column.id === null ? "" : String(column.id)}
              onChange={(event) => actions.moveCard(card.id, event.target.value)}
            >
              <option value="">Ungrouped</option>
              {clusters.map((cluster) => (
                <option key={cluster.id} value={String(cluster.id)}>
                  {cluster.name}
                </option>
              ))}
            </select>
          </label>
          {inCluster && (
            <label className="board-check">
              <input
                type="checkbox"
                checked={selected.has(card.id)}
                onChange={() => actions.toggleSelected(card.id)}
              />
              Select to split
            </label>
          )}
        </p>
      )}
    </li>
  );
}

function ClusterActions({ column, clusters, canShape, selectedHere, ui, actions }) {
  if (!canShape || column.id === null) {
    return null;
  }
  const others = clusters.filter((cluster) => cluster.id !== column.id);
  return (
    <p className="board-actions">
      <button type="button" className="btn-secondary" onClick={() => actions.startRename(column)}>
        Rename
      </button>
      {others.length > 0 && (
        <label className="board-select">
          Merge into
          <select
            value=""
            onChange={(event) => actions.merge(column.id, event.target.value)}
          >
            <option value="">Choose a cluster…</option>
            {others.map((cluster) => (
              <option key={cluster.id} value={String(cluster.id)}>
                {cluster.name}
              </option>
            ))}
          </select>
        </label>
      )}
      <button
        type="button"
        className="btn-secondary"
        disabled={selectedHere.length === 0}
        onClick={() => actions.split(column.id, selectedHere)}
      >
        Split selected off ({selectedHere.length})
      </button>
      {ui.confirmingDelete === column.id ? (
        <span className="board-actions">
          <button type="button" className="btn-secondary" onClick={() => actions.delete(column.id)}>
            Confirm delete
          </button>
          <button type="button" className="btn-secondary" onClick={actions.cancelDelete}>
            Keep it
          </button>
        </span>
      ) : (
        <button type="button" className="btn-secondary" onClick={() => actions.askDelete(column.id)}>
          Delete
        </button>
      )}
    </p>
  );
}

function Column({ column, cards, clusters, canShape, selected, ui, actions }) {
  // `column.id === null` is the ungrouped column; guard against it colliding
  // with the `renamingId: null` idle state, which would put it in rename mode.
  const renaming = column.id !== null && ui.renamingId === column.id;
  const selectedHere = cards.filter((card) => selected.has(card.id)).map((card) => card.id);
  return (
    <section
      className="board-column"
      onDragOver={canShape ? (event) => event.preventDefault() : undefined}
      onDrop={canShape ? (event) => actions.drop(event, column.id) : undefined}
    >
      <div className="board-column-head">
        {renaming ? (
          <span className="board-select">
            <input
              className="board-input"
              value={ui.renameDraft}
              aria-label="New cluster name"
              onChange={(event) => actions.setRenameDraft(event.target.value)}
            />
            <button type="button" className="btn-secondary" onClick={() => actions.rename(column.id)}>
              Save
            </button>
            <button type="button" className="btn-secondary" onClick={actions.cancelRename}>
              Cancel
            </button>
          </span>
        ) : (
          <span className="board-column-title">
            {column.name}
            {column.suggested && <span className="board-tag">Suggested</span>}
          </span>
        )}
      </div>
      <ClusterActions
        column={column}
        clusters={clusters}
        canShape={canShape}
        selectedHere={selectedHere}
        ui={ui}
        actions={actions}
      />
      <ul className="board-cards">
        {cards.length === 0 && <li className="board-note">No cards here yet.</li>}
        {cards.map((card) => (
          <Card
            key={card.id}
            card={card}
            column={column}
            canShape={canShape}
            clusters={clusters}
            selected={selected}
            actions={actions}
          />
        ))}
      </ul>
    </section>
  );
}

function CreateCluster({ onCreate }) {
  const [name, setName] = useState("");
  const submit = (event) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      return;
    }
    onCreate(trimmed);
    setName("");
  };
  return (
    <form className="board-toolbar" onSubmit={submit}>
      <input
        className="board-input"
        value={name}
        placeholder="New cluster name"
        aria-label="New cluster name"
        onChange={(event) => setName(event.target.value)}
      />
      <button type="submit" className="btn-primary">
        Add cluster
      </button>
    </form>
  );
}

function stageNote(stage) {
  if (stage === "DRAFT") {
    return "The cards have not been revealed yet, so there is nothing to cluster.";
  }
  return "Clustering is closed. The board can no longer be changed.";
}

function Board({ bootstrap }) {
  const { urls } = bootstrap;
  const csrfToken = useRef(readCsrfToken());

  const [board, setBoard] = useState(null);
  const [fatal, setFatal] = useState(null);
  const [syncError, setSyncError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [ui, setUi] = useState({
    renamingId: null,
    renameDraft: "",
    confirmingDelete: null,
  });

  // Refs the poll loop reads. Kept out of state so the effect can run once and
  // still see the latest values without re-subscribing.
  const versionRef = useRef(-1);
  const draggingRef = useRef(false);
  const pendingRef = useRef(null);
  const fatalRef = useRef(false);
  const dragCardRef = useRef(null);

  const resetTransientUi = useCallback(() => {
    setSelected(new Set());
    setUi({ renamingId: null, renameDraft: "", confirmingDelete: null });
  }, []);

  // Replace the board outright — the whole state, never a merge. Guarded so
  // a poll that raced a mutation and came back a version behind cannot undo it.
  const commit = useCallback(
    (state) => {
      if (state.version < versionRef.current) {
        return;
      }
      versionRef.current = state.version;
      setBoard(state);
      resetTransientUi();
    },
    [resetTransientUi],
  );

  // A poll's new state, applied unless the user is mid-drag: a board yanked out
  // from under a dragging hand is the thing the mid-drag rule forbids. It is
  // stashed and applied the moment the drag finishes.
  const applyIncoming = useCallback(
    (state) => {
      if (draggingRef.current) {
        pendingRef.current = state;
        return;
      }
      commit(state);
    },
    [commit],
  );

  const flushPending = useCallback(() => {
    if (!draggingRef.current && pendingRef.current !== null) {
      const state = pendingRef.current;
      pendingRef.current = null;
      commit(state);
    }
  }, [commit]);

  const goFatal = useCallback((message) => {
    fatalRef.current = true;
    setFatal(message);
  }, []);

  // Every write posts to one of #12's endpoints and gets the whole new board
  // back, which is applied directly rather than waiting for the next poll. A
  // failure leaves the last known-good board on screen and shows why.
  const mutate = useCallback(
    async (url, body) => {
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "X-CSRFToken": csrfToken.current, Accept: "application/json" },
          body,
          credentials: "same-origin",
        });
        if (response.status === 403 || response.status === 404) {
          goFatal(MESSAGES.gone);
          return;
        }
        const data = await response.json().catch(() => null);
        if (!response.ok) {
          setActionError((data && data.error) || MESSAGES.action);
          return;
        }
        setActionError(null);
        commit(data);
      } catch {
        setActionError(MESSAGES.offline);
      }
    },
    [commit, goFatal],
  );

  // The poll loop. It schedules the next request only once the last one has
  // settled, so a slow or failing response delays the next attempt instead of
  // letting requests pile up: an error never turns into a storm. It pauses while
  // the tab is hidden and resumes on the way back, and it stops for good once
  // the board is gone.
  useEffect(() => {
    let cancelled = false;
    let timer = null;

    const schedule = () => {
      if (!cancelled && !fatalRef.current) {
        timer = setTimeout(tick, POLL_INTERVAL);
      }
    };

    async function tick() {
      if (cancelled || fatalRef.current) {
        return;
      }
      if (typeof document !== "undefined" && document.hidden) {
        schedule();
        return;
      }
      try {
        const known = versionRef.current;
        const url = known >= 0 ? `${urls.state}?v=${known}` : urls.state;
        const response = await fetch(url, {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        if (response.status === 403 || response.status === 404) {
          goFatal(MESSAGES.gone);
          return;
        }
        if (!response.ok) {
          setSyncError(MESSAGES.refresh);
          schedule();
          return;
        }
        const data = await response.json();
        if (data.changed) {
          applyIncoming(data);
        }
        setSyncError(null);
        schedule();
      } catch {
        setSyncError(MESSAGES.offline);
        schedule();
      }
    }

    const onVisibility = () => {
      if (!document.hidden && !fatalRef.current) {
        clearTimeout(timer);
        tick();
      }
    };

    tick();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [urls, applyIncoming, goFatal]);

  const form = (fields) => {
    const body = new FormData();
    for (const [key, value] of fields) {
      body.append(key, value);
    }
    return body;
  };

  const actions = {
    dragStart: (event, cardId) => {
      draggingRef.current = true;
      dragCardRef.current = cardId;
      event.dataTransfer.effectAllowed = "move";
    },
    dragEnd: () => {
      draggingRef.current = false;
      dragCardRef.current = null;
      flushPending();
    },
    drop: (event, clusterId) => {
      event.preventDefault();
      const cardId = dragCardRef.current;
      draggingRef.current = false;
      dragCardRef.current = null;
      pendingRef.current = null;
      if (cardId !== null) {
        actions.moveCard(cardId, clusterId === null ? "" : String(clusterId));
      }
    },
    moveCard: (cardId, clusterValue) => {
      if (clusterValue === "") {
        mutate(urls.cardUngroup, form([["card", cardId]]));
      } else {
        mutate(urls.cardMove, form([["card", cardId], ["cluster", clusterValue]]));
      }
    },
    createCluster: (name) => mutate(urls.clusterCreate, form([["name", name]])),
    startRename: (column) =>
      setUi({ renamingId: column.id, renameDraft: column.name, confirmingDelete: null }),
    setRenameDraft: (value) => setUi((current) => ({ ...current, renameDraft: value })),
    cancelRename: () => setUi((current) => ({ ...current, renamingId: null, renameDraft: "" })),
    rename: (clusterId) => {
      const name = ui.renameDraft.trim();
      setUi((current) => ({ ...current, renamingId: null, renameDraft: "" }));
      if (name.length > 0) {
        mutate(urls.clusterRename, form([["cluster", String(clusterId)], ["name", name]]));
      }
    },
    merge: (sourceId, targetValue) => {
      if (targetValue !== "") {
        mutate(urls.clusterMerge, form([["source", String(sourceId)], ["target", targetValue]]));
      }
    },
    toggleSelected: (cardId) =>
      setSelected((current) => {
        const next = new Set(current);
        if (next.has(cardId)) {
          next.delete(cardId);
        } else {
          next.add(cardId);
        }
        return next;
      }),
    split: (sourceId, cardIds) => {
      if (cardIds.length > 0) {
        mutate(
          urls.clusterSplit,
          form([["cluster", String(sourceId)], ...cardIds.map((id) => ["cards", id])]),
        );
      }
    },
    askDelete: (clusterId) => setUi((current) => ({ ...current, confirmingDelete: clusterId })),
    cancelDelete: () => setUi((current) => ({ ...current, confirmingDelete: null })),
    delete: (clusterId) => {
      setUi((current) => ({ ...current, confirmingDelete: null }));
      mutate(urls.clusterDelete, form([["cluster", String(clusterId)]]));
    },
  };

  if (fatal !== null) {
    return (
      <>
        <h2 className="section-heading">The board</h2>
        <p className="board-banner">{fatal}</p>
      </>
    );
  }

  if (board === null) {
    return (
      <>
        <h2 className="section-heading">The board</h2>
        <p className="board-note">Loading the board…</p>
      </>
    );
  }

  const canShape = SHAPING_STAGES.has(board.stage);
  const clusters = board.clusters.map((cluster) => ({
    id: cluster.id,
    name: cluster.name,
    suggested: cluster.is_auto_generated,
  }));
  const grouped = groupCards(board.cards);
  const columns = [{ id: null, name: "Ungrouped", suggested: false }, ...clusters];
  const banner = actionError ?? syncError;

  return (
    <>
      <h2 className="section-heading">The board</h2>
      {banner && <p className="board-banner">{banner}</p>}
      {canShape ? (
        <CreateCluster onCreate={actions.createCluster} />
      ) : (
        <p className="board-note">{stageNote(board.stage)}</p>
      )}
      <div className="board-columns">
        {columns.map((column) => (
          <Column
            key={column.id === null ? "ungrouped" : column.id}
            column={column}
            cards={grouped.get(column.id) ?? []}
            clusters={clusters}
            canShape={canShape}
            selected={selected}
            ui={ui}
            actions={actions}
          />
        ))}
      </div>
    </>
  );
}

const mount = document.getElementById(MOUNT_ID);
if (mount === null) {
  throw new Error(`No #${MOUNT_ID} element on the page: the island has nowhere to mount.`);
}

createRoot(mount).render(
  <StrictMode>
    <Board bootstrap={readBootstrap()} />
  </StrictMode>,
);
