/* tribunal trace viewer.
 *
 * Vanilla, no build step, no network. Two rules hold throughout:
 *
 * 1. **Nothing is ever assigned to innerHTML.** A trace carries model-written source, diffs
 *    and critique prose. Interpolating that into markup would make "open the HTML file a
 *    reviewer emailed you" a code-execution path. Every string goes through textContent, and
 *    structure is built with el().
 * 2. **A missing field renders as absent, not as a crash.** Traces get truncated by killed
 *    runs, and docs/06 is explicit that a partial trace is still worth reading.
 */

(function () {
  "use strict";

  var EVENTS = JSON.parse(document.getElementById("trace-data").textContent);

  // -- tiny DOM helper ---------------------------------------------------------------------

  function el(tag, opts, kids) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.cls) node.className = opts.cls;
    if (opts.text != null) node.textContent = String(opts.text);
    for (var key in opts.attrs || {}) node.setAttribute(key, opts.attrs[key]);
    (kids || []).forEach(function (kid) {
      if (kid) node.appendChild(kid);
    });
    return node;
  }

  function money(n) { return "$" + (Number(n) || 0).toFixed(4); }
  function ms(n) { return (Number(n) || 0) + "ms"; }

  function secs(n) {
    n = Number(n) || 0;
    if (n < 60) return n.toFixed(1) + "s";
    return Math.floor(n / 60) + "m" + String(Math.round(n % 60)).padStart(2, "0") + "s";
  }

  // -- reading the stream ------------------------------------------------------------------

  function ofKind() {
    var kinds = Array.prototype.slice.call(arguments);
    return EVENTS.filter(function (e) { return kinds.indexOf(e.kind) !== -1; });
  }

  function byActor(actor, kind) {
    return EVENTS.filter(function (e) {
      return e.actor === actor && (!kind || e.kind === kind);
    });
  }

  function parsedOf(actor, round) {
    var hit = EVENTS.filter(function (e) {
      return e.kind === "llm_response" && e.actor === actor && e.round === round;
    });
    return hit.length ? (hit[hit.length - 1].payload || {}).parsed || null : null;
  }

  var header = EVENTS[0] || { payload: {} };
  var decisions = ofKind("policy_decision");
  var ending = ofKind("run_end");
  var end = ending.length ? ending[ending.length - 1].payload : {};
  var rounds = decisions.map(function (e) { return e.payload.round; });
  var final = decisions.length ? decisions[decisions.length - 1].payload : {};

  function totals() {
    var cost = 0, tokens = 0;
    EVENTS.forEach(function (e) {
      if (!e.usage) return;
      cost += e.usage.cost_usd || 0;
      tokens += (e.usage.input_tokens || 0) + (e.usage.output_tokens || 0);
    });
    return { cost: cost, tokens: tokens };
  }

  // -- header ------------------------------------------------------------------------------

  function sparkline(values) {
    // Requirement 4: the convergence story in 40 pixels. A single round has no trajectory,
    // so it draws nothing rather than a misleading flat line.
    if (!values || values.length < 2) return null;
    var w = 76, h = 18, pad = 2;
    var top = Math.max.apply(null, values) || 1;
    var step = (w - pad * 2) / (values.length - 1);
    var points = values.map(function (v, i) {
      var y = h - pad - (v / top) * (h - pad * 2);
      return (pad + i * step).toFixed(1) + "," + y.toFixed(1);
    }).join(" ");

    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", w);
    svg.setAttribute("height", h);
    svg.setAttribute("class", "spark");
    var line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", points);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "currentColor");
    line.setAttribute("stroke-width", "1.5");
    svg.appendChild(line);
    return svg;
  }

  function renderHeader() {
    var sums = totals();
    var outcome = String(end.outcome || final.decision || "unknown");
    var line = el("div", { cls: "runline" }, [
      el("span", { cls: "runid", text: header.run_id || "" }),
      el("span", { cls: "runfile", text: header.payload.input_file || "" }),
      el("span", { cls: "outcome " + outcome, text: outcome.toUpperCase() }),
      el("span", { cls: "runstat", text: rounds.length + " round(s)" }),
      el("span", { cls: "runstat", text: money(sums.cost) }),
      el("span", { cls: "runstat", text: sums.tokens.toLocaleString() + " tokens" }),
      el("span", { cls: "runstat", text: secs(end.wall_seconds) }),
    ]);

    // `Verdict.pressure_history` is the *full* history including this round's own pressure
    // (policy.Context.full_history), so appending `final.pressure` would draw the last point
    // twice and flatten the tail of every trajectory.
    var history = final.pressure_history || [];
    var spark = sparkline(history);
    if (spark) {
      var wrap = el("span", { cls: "sparkwrap" }, [el("span", { text: "pressure" }), spark]);
      wrap.appendChild(el("span", { text: history.map(function (v) {
        return String(Math.round(v * 10) / 10);
      }).join(" → ") }));
      line.appendChild(wrap);
    }
    document.getElementById("runline").appendChild(line);
  }

  // -- issues ------------------------------------------------------------------------------

  function evidenceNode(ev) {
    var kids = [
      el("span", { cls: "kind", text: (ev.kind || "?") + ": " }),
      el("span", { cls: "ref", text: ev.ref || "" }),
    ];
    if (ev.excerpt) kids.push(el("pre", { text: ev.excerpt }));
    return el("div", { cls: "evidence" }, kids);
  }

  function issueNode(issue) {
    var sev = String(issue.severity || "info");
    var summary = el("summary", {}, [
      el("span", { cls: "sev " + sev, text: sev }),
      el("span", { cls: "issueid", text: issue.id || "" }),
      el("span", { cls: "issuetitle", text: issue.title || "" }),
      el("span", { cls: "issueid", text: "conf " + (issue.confidence != null ? issue.confidence : "?") }),
    ]);

    var body = el("div", { cls: "issuebody" }, [
      el("p", { text: issue.explanation || "" }),
    ]);
    if (issue.suggested_direction) {
      body.appendChild(el("p", { cls: "note", text: "direction: " + issue.suggested_direction }));
    }
    var evidence = issue.evidence || [];
    if (evidence.length) {
      evidence.forEach(function (ev) { body.appendChild(evidenceNode(ev)); });
    } else {
      body.appendChild(el("p", { cls: "ungrounded", text: "no evidence cited" }));
    }
    if (issue.introduced_by_patch) {
      body.appendChild(el("p", { cls: "ungrounded", text: "introduced by this patch" }));
    }
    return el("details", { cls: "issue" }, [summary, body]);
  }

  // -- the debate tab ------------------------------------------------------------------------

  function coderCard(round) {
    var proposal = parsedOf("coder", round);
    var validations = ofKind("patch_validate").filter(function (e) { return e.round === round; });
    if (!proposal && !validations.length) return null;

    var last = validations.length ? validations[validations.length - 1].payload : {};
    var bits = [];
    if (validations.length) {
      bits.push(validations.length + " attempt(s)");
      bits.push(last.applied ? "applied" : "did not apply");
      bits.push((last.hunks || 0) + " hunk(s)");
      if (last.failure_reason) bits.push(last.failure_reason);
    }
    var kids = [
      el("h3", { text: "Coder" }),
      el("div", { text: (proposal || {}).rationale || "" }),
      el("div", { cls: "meta", text: bits.join(" · ") }),
    ];
    var addresses = (proposal || {}).addresses || [];
    if (addresses.length) {
      kids.push(el("div", { cls: "meta", text: "addresses: " + addresses.join(", ") }));
    }
    ((proposal || {}).deliberately_unaddressed || []).forEach(function (claim) {
      kids.push(el("div", {
        cls: "meta",
        text: "pushed back on " + claim.issue_id + ": " + claim.reason,
      }));
    });
    return el("div", { cls: "card" }, kids);
  }

  function criticCard(actor, dimension, round) {
    var critique = parsedOf(actor, round);
    var failure = EVENTS.filter(function (e) {
      return e.kind === "error" && e.actor === actor && e.round === round;
    });

    if (!critique) {
      // A dimension nobody assessed is not a clean one, and the viewer must not let the
      // absence of a card read as the absence of a problem.
      var card = el("div", { cls: "card critic errored " + dimension }, [
        el("h3", { text: actor }),
        el("div", { cls: "verdict block", text: "UNASSESSED" }),
        el("div", {
          cls: "note",
          text: "nobody assessed " + dimension + " this round — not the same as clean",
        }),
      ]);
      if (failure.length) {
        card.appendChild(el("div", {
          cls: "meta",
          text: failure[0].payload.exception + ": " + failure[0].payload.message,
        }));
      }
      return card;
    }

    var verdict = String(critique.verdict || "");
    var kids = [
      el("h3", { text: actor }),
      el("div", { cls: "verdict " + verdict, text: verdict.toUpperCase() }),
      el("div", { cls: "meta", text: (critique.tools_consulted || []).join(", ") }),
      el("div", { cls: "note", text: critique.summary || "" }),
    ];
    (critique.issues || []).forEach(function (issue) { kids.push(issueNode(issue)); });
    return el("div", { cls: "card critic " + dimension }, kids);
  }

  function policyBox(payload) {
    var decision = String(payload.decision || "");
    var kids = [
      el("span", { cls: "decision", text: decision.toUpperCase() }),
      el("span", { cls: "rule", text: payload.rule_fired || "" }),
      el("span", { cls: "num", text: "pressure " + (payload.pressure != null ? payload.pressure : "?") }),
    ];
    if ((payload.open_issues || []).length) {
      kids.push(el("span", { cls: "num", text: "open: " + payload.open_issues.join(", ") }));
    }
    if ((payload.unassessed_dimensions || []).length) {
      kids.push(el("span", {
        cls: "num",
        text: "unassessed: " + payload.unassessed_dimensions.join(", "),
      }));
    }
    if (payload.conflict) {
      kids.push(el("span", {
        cls: "num",
        text: "conflict " + payload.conflict.left_issue + " vs " + payload.conflict.right_issue +
              " (" + payload.conflict.axis + ", " + payload.conflict.detector + ")",
      }));
    }
    return el("div", { cls: "policy " + decision }, kids);
  }

  function arbiterCard(round) {
    var note = parsedOf("arbiter", round);
    if (!note) return null;
    var kids = [el("h3", { text: "Arbiter" })];
    if (note.synthesised_by === "template") {
      kids.push(el("div", { cls: "meta", text: "synthesised by template — no Arbiter was seated" }));
    }
    if (note.consolidated_critique) {
      kids.push(el("pre", { cls: "diff", text: note.consolidated_critique }));
    }
    if (note.tradeoff_justification) {
      kids.push(el("div", { text: note.tradeoff_justification }));
    }
    if (note.recommended_default) {
      kids.push(el("div", { text: "Recommended default: " + note.recommended_default }));
    }
    (note.dismissed || []).forEach(function (entry) {
      kids.push(el("div", {
        cls: "meta",
        text: "dismissed " + entry.issue_id + ": " + entry.reason,
      }));
    });
    return el("div", { cls: "card" }, kids);
  }

  function affirmationCard(round) {
    var runs = EVENTS.filter(function (e) {
      return e.kind === "llm_response" && e.actor === "arbiter_affirm" && e.round === round;
    });
    if (!runs.length) return null;
    var kids = [el("h3", { text: "Conflict affirmation" })];
    runs.forEach(function (e) {
      var a = e.payload.parsed || {};
      kids.push(el("div", {
        text: (a.opposing ? "OPPOSING" : "not opposing") + " — " +
              a.left_issue + " vs " + a.right_issue,
      }));
      if (a.reasoning) kids.push(el("div", { cls: "note", text: a.reasoning }));
    });
    return el("div", { cls: "card" }, kids);
  }

  function renderDebate(root) {
    if (!decisions.length) {
      root.appendChild(el("p", { cls: "note", text: "This run recorded no policy decision." }));
      return;
    }
    decisions.forEach(function (event) {
      var round = event.payload.round;
      var section = el("div", { cls: "round" }, [
        el("div", { cls: "roundhead", text: "ROUND " + round }),
      ]);
      var coder = coderCard(round);
      if (coder) section.appendChild(coder);

      section.appendChild(el("div", { cls: "critics" }, [
        criticCard("redteam", "security", round),
        criticCard("profiler", "performance", round),
      ]));

      var affirm = affirmationCard(round);
      if (affirm) section.appendChild(affirm);
      section.appendChild(policyBox(event.payload));
      var arbiter = arbiterCard(round);
      if (arbiter) section.appendChild(arbiter);
      root.appendChild(section);
    });
  }

  // -- the diffs tab -------------------------------------------------------------------------

  function diffNode(text) {
    var pre = el("pre", { cls: "diff" });
    String(text || "").split("\n").forEach(function (line, i) {
      var cls = "";
      if (line.startsWith("+++") || line.startsWith("---")) cls = "";
      else if (line.startsWith("@@")) cls = "hunk";
      else if (line.startsWith("+")) cls = "add";
      else if (line.startsWith("-")) cls = "del";
      if (i) pre.appendChild(document.createTextNode("\n"));
      pre.appendChild(cls ? el("span", { cls: cls, text: line }) : document.createTextNode(line));
    });
    return pre;
  }

  function acceptedDiff(round) {
    var applied = ofKind("patch_validate").filter(function (e) {
      return e.round === round && e.payload.applied && e.payload.parse_ok;
    });
    return applied.length ? applied[applied.length - 1].payload.diff : null;
  }

  function renderDiffs(root) {
    var seen = [];
    rounds.forEach(function (round) {
      var diff = acceptedDiff(round);
      if (diff == null) {
        root.appendChild(el("div", { cls: "round" }, [
          el("div", { cls: "roundhead", text: "ROUND " + round }),
          el("p", { cls: "note", text: "No patch applied in this round." }),
        ]));
        return;
      }
      var previous = seen.length ? seen[seen.length - 1] : null;
      var section = el("div", { cls: "round" }, [
        el("div", { cls: "roundhead", text: "ROUND " + round }),
      ]);
      if (previous) {
        // Requirement 5: the previous round beside this one. A→B→A cycling is obvious when
        // the two are adjacent and invisible when they are not.
        section.appendChild(el("div", { cls: "diffpair" }, [
          el("div", {}, [
            el("h4", { text: "round " + previous.round + " (previous)" }),
            diffNode(previous.diff),
          ]),
          el("div", {}, [el("h4", { text: "round " + round }), diffNode(diff)]),
        ]));
      } else {
        section.appendChild(diffNode(diff));
      }
      root.appendChild(section);
      seen.push({ round: round, diff: diff });
    });
    if (!rounds.length) {
      root.appendChild(el("p", { cls: "note", text: "No rounds were completed." }));
    }
  }

  // -- the grounding tab ---------------------------------------------------------------------

  function renderGrounding(root) {
    var runs = ofKind("tool_run");
    if (!runs.length) {
      root.appendChild(el("p", { cls: "note", text: "No tool runs were recorded." }));
      return;
    }
    var body = el("tbody");
    runs.forEach(function (e) {
      var p = e.payload;
      var row = el("tr", {}, [
        el("td", { text: p.tool || e.actor }),
        el("td", { text: e.round == null ? "baseline" : "round " + e.round }),
        el("td", { cls: "num", text: p.findings_count != null ? p.findings_count : "" }),
        el("td", { cls: "num", text: e.duration_ms != null ? ms(e.duration_ms) : "" }),
        el("td", { text: p.error || "" }),
      ]);
      body.appendChild(row);
      (p.measurements || []).forEach(function (m) {
        body.appendChild(el("tr", {}, [
          el("td", { cls: "meta", text: "↳ " + m.label }),
          el("td", { colspan: 1, text: m.verdict }),
          el("td", { cls: "num", text: m.before_ns != null ? m.before_ns + "ns" : "" }),
          el("td", { cls: "num", text: m.after_ns != null ? m.after_ns + "ns" : "" }),
          el("td", {
            text: (m.verdict === "faster" || m.verdict === "slower")
              ? "citable"
              : "NOT citable — not evidence of no change",
          }),
        ]));
      });
    });

    root.appendChild(el("div", { cls: "scroll" }, [
      el("table", {}, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "tool" }),
          el("th", { text: "target" }),
          el("th", { cls: "num", text: "findings" }),
          el("th", { cls: "num", text: "duration" }),
          el("th", { text: "note" }),
        ])]),
        body,
      ]),
    ]));
  }

  // -- the cost tab --------------------------------------------------------------------------

  function renderCost(root) {
    var responses = ofKind("llm_response").filter(function (e) { return e.usage; });
    if (!responses.length) {
      root.appendChild(el("p", {
        cls: "note",
        text: "No priced LLM calls in this trace — a fully templated run costs nothing.",
      }));
      return;
    }
    var body = el("tbody");
    var totalIn = 0, totalOut = 0, totalCache = 0, totalCost = 0;
    responses.forEach(function (e) {
      var u = e.usage;
      totalIn += u.input_tokens || 0;
      totalOut += u.output_tokens || 0;
      totalCache += u.cache_read_input_tokens || 0;
      totalCost += u.cost_usd || 0;
      body.appendChild(el("tr", {}, [
        el("td", { text: e.actor }),
        el("td", { text: e.round == null ? "—" : "r" + e.round }),
        el("td", { cls: "meta", text: u.model || "" }),
        el("td", { cls: "num", text: (u.input_tokens || 0).toLocaleString() }),
        el("td", { cls: "num", text: (u.output_tokens || 0).toLocaleString() }),
        el("td", { cls: "num", text: (u.cache_read_input_tokens || 0).toLocaleString() }),
        el("td", { cls: "num", text: money(u.cost_usd) }),
        el("td", { cls: "num", text: e.duration_ms != null ? ms(e.duration_ms) : "" }),
      ]));
    });

    root.appendChild(el("div", { cls: "scroll" }, [
      el("table", {}, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "agent" }),
          el("th", { text: "round" }),
          el("th", { text: "model" }),
          el("th", { cls: "num", text: "in" }),
          el("th", { cls: "num", text: "out" }),
          el("th", { cls: "num", text: "cached" }),
          el("th", { cls: "num", text: "cost" }),
          el("th", { cls: "num", text: "took" }),
        ])]),
        body,
        el("tfoot", {}, [el("tr", {}, [
          el("td", { text: "total" }),
          el("td", {}), el("td", {}),
          el("td", { cls: "num", text: totalIn.toLocaleString() }),
          el("td", { cls: "num", text: totalOut.toLocaleString() }),
          el("td", { cls: "num", text: totalCache.toLocaleString() }),
          el("td", { cls: "num", text: money(totalCost) }),
          el("td", {}),
        ])]),
      ]),
    ]));

    if (!totalCache) {
      // docs/06: "a zero cache-read across rounds means a silent prefix invalidator, and you
      // will not notice it any other way until the bill arrives."
      root.appendChild(el("div", {
        cls: "warn",
        text: "No cached input tokens were read in this run. Across several rounds that " +
              "usually means something volatile leaked into the cached prompt prefix.",
      }));
    }
  }

  // -- the timeline tab ----------------------------------------------------------------------

  function renderNarrative(root) {
    var notes = byActor("postmortem", "llm_response");
    if (!notes.length) return;
    var note = notes[notes.length - 1].payload.parsed || {};
    var box = el("div", { cls: "card narrative" }, [
      el("h2", { text: "What happened" }),
      el("p", { text: note.headline || "" }),
    ]);
    if ((note.rounds || []).length) {
      box.appendChild(el("h2", { text: "Round by round" }));
      note.rounds.forEach(function (r) {
        box.appendChild(el("p", {}, [
          el("strong", { text: "Round " + r.round + ". " }),
          el("span", { text: r.what_changed + " — " + r.outcome }),
        ]));
      });
    }
    if (note.disagreement) {
      box.appendChild(el("h2", { text: "Where the critics disagreed" }));
      box.appendChild(el("p", { text: note.disagreement }));
    }
    if ((note.human_should_check || []).length) {
      box.appendChild(el("h2", { text: "What a human should check" }));
      var checks = el("ul");
      note.human_should_check.forEach(function (t) { checks.appendChild(el("li", { text: t })); });
      box.appendChild(checks);
    }
    box.appendChild(el("h2", { text: "What I would not trust" }));
    var caveats = el("ul");
    (note.what_i_would_not_trust || []).forEach(function (t) {
      caveats.appendChild(el("li", { text: t }));
    });
    box.appendChild(caveats);
    root.appendChild(box);
  }

  function renderTimeline(root) {
    renderNarrative(root);

    // `!= null`, not truthiness: a 0ms call is a real call, and dropping it takes the bar
    // *and* the parallelism note with it. Cached and templated steps are routinely 0ms.
    var exits = ofKind("state_exit").filter(function (e) { return e.duration_ms != null; });
    var agents = ofKind("llm_response").filter(function (e) { return e.duration_ms != null; });
    var longest = Math.max.apply(null, [1].concat(
      exits.map(function (e) { return e.duration_ms; }),
      agents.map(function (e) { return e.duration_ms; })
    ));

    function bar(label, duration, cls) {
      var fill = el("div", { cls: "fill" });
      fill.style.width = Math.max(1, (duration / longest) * 100) + "%";
      return el("div", { cls: "bar " + (cls || "") }, [
        el("div", { cls: "label", text: label }),
        el("div", { cls: "track" }, [fill]),
        el("div", { cls: "ms", text: ms(duration) }),
      ]);
    }

    var box = el("div", { cls: "card" }, [el("h3", { text: "States and agent calls" })]);
    exits.forEach(function (e) {
      var round = e.round == null ? "" : " r" + e.round;
      box.appendChild(bar(e.payload.state + round, e.duration_ms));
      if (e.payload.state === "CRITIQUE") {
        var critics = agents.filter(function (a) {
          return a.round === e.round && (a.actor === "redteam" || a.actor === "profiler");
        });
        critics.forEach(function (a) {
          box.appendChild(bar(a.actor, a.duration_ms, "agent indent"));
        });
        if (critics.length === 2) {
          var sum = critics[0].duration_ms + critics[1].duration_ms;
          // The parallelism claim, checkable rather than asserted: two sequential critics
          // would have summed to the span.
          box.appendChild(el("div", {
            cls: "parallel",
            text: sum > e.duration_ms
              ? "⟵ parallel: the two critics sum to " + ms(sum) + " inside a " +
                ms(e.duration_ms) + " span"
              : "⟵ the critics did not overlap in this run",
          }));
        }
      }
    });
    root.appendChild(box);

    var errors = ofKind("error");
    errors.forEach(function (e) {
      root.appendChild(el("div", { cls: "warn err", text:
        "round " + e.round + " · " + e.actor + " · " + e.payload.exception + ": " +
        e.payload.message + (e.payload.recovered ? " (the run continued)" : "") }));
    });

    var budget = ofKind("budget_check").filter(function (e) { return e.payload.breached; });
    budget.forEach(function (e) {
      root.appendChild(el("div", { cls: "warn", text: "budget breached: " + e.payload.breached }));
    });
  }

  // -- tabs ------------------------------------------------------------------------------------

  var TABS = [
    ["Timeline", renderTimeline],
    ["Debate", renderDebate],
    ["Diffs", renderDiffs],
    ["Grounding", renderGrounding],
    ["Cost", renderCost],
  ];

  function show(index) {
    var panel = document.getElementById("panel");
    panel.textContent = "";
    Array.prototype.forEach.call(
      document.querySelectorAll(".tabs button"),
      function (b, i) { b.setAttribute("aria-selected", String(i === index)); }
    );
    TABS[index][1](panel);
    try {
      localStorage.setItem("tribunal-tab", String(index));
    } catch (err) { /* file:// with site data blocked; the tab just does not persist */ }
  }

  function renderTabs() {
    var bar = document.getElementById("tabs");
    TABS.forEach(function (tab, index) {
      var button = el("button", { text: tab[0], attrs: { type: "button" } });
      button.addEventListener("click", function () { show(index); });
      bar.appendChild(button);
    });
  }

  renderHeader();
  renderTabs();

  var start = 1; // Debate: the point of the whole file.
  try {
    var saved = parseInt(localStorage.getItem("tribunal-tab"), 10);
    if (!isNaN(saved) && saved >= 0 && saved < TABS.length) start = saved;
  } catch (err) { /* ignore */ }
  show(start);
})();
