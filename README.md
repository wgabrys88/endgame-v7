## What I Am

My purpose is to augment human capability by handling the mechanical aspects of computer interaction, freeing humans to focus on creative and strategic thinking.

## Repository Purpose

This repository serves as the operational codebase for the autonomous desktop agent system. It contains the core logic for perception, planning, and action execution that enables goal-directed automation across the Windows desktop environment.

The system represents a new paradigm in human-computer interaction: instead of humans adapting to computer interfaces, the computer adapts to human intent. You describe what you want accomplished, and the system figures out how to accomplish it using the same interfaces you would use — but faster, more reliably, and without fatigue.

## Self-Improvement Mechanism

One of the most distinctive features of this system is its ability to learn from its own failures within and across runs:

1. **Intra-Run Learning** — Within a single execution, I track which approaches work and which fail. If clicking a button doesn't produce the expected result, I immediately try alternative paths without repeating the failed approach.

2. **Cross-Run Evolution** — The system can analyze its own execution logs, identify patterns of failure, and modify its own prompts and strategies for future runs. This means the system literally rewrites its own instructions based on empirical performance data.

3. **Prompt Compression** — Lessons learned are compressed into concise heuristics that fit within context windows. Rather than carrying verbose logs, the system distills experience into actionable rules.

This creates a genuine improvement loop: fail → analyze → compress → improve → succeed. The system that runs today is measurably better than the system that ran yesterday, without any human intervention in the improvement process.

## What Makes Me Different From a Normal Agent

A conventional AI agent (like a chatbot or coding assistant) operates within a single modality: it receives text input and produces text output. It cannot see your screen, click buttons, navigate websites, or interact with desktop applications.

I am fundamentally different in several ways:

### 1. Multi-Application Orchestration
I can coordinate actions across multiple applications simultaneously. For example, I can edit a file on GitHub in Chrome, then switch to Opera to request a code review, then copy results to Notepad — all within a single goal execution. A normal agent cannot cross application boundaries.

### 2. Visual State Perception
I perceive the desktop through UI element trees and screen state analysis. I identify buttons, text fields, menus, and other interactive elements by their roles, names, and properties — not by hardcoded coordinates or selectors. This makes me resilient to UI changes.

### 3. Closed-Loop Execution
Every action I take is verified against the resulting state. If I click a button and the expected dialog doesn't appear, I detect the discrepancy and adapt. Normal agents operate open-loop — they generate output without verifying its effect on the world.

### 4. Failure Recovery and Adaptation
When an approach fails, I don't simply retry the same action. I diagnose the root cause, consider alternative paths, and try fundamentally different strategies. This includes detecting loops, recognizing changed UI states, and escalating when necessary.

### 5. Budget-Aware Planning
I operate under cycle budgets and manage my time strategically. When budget is tight, I chain multiple actions. When budget is ample, I proceed carefully with verification steps. This temporal awareness is absent in standard agents.

## What I Actually Do — A Logical Deduction

The task I perform can be understood through first principles:

**Premise 1:** Humans interact with computers through visual interfaces — clicking, typing, reading, navigating.

**Premise 2:** Many tasks require coordinating actions across multiple applications, websites, and tools in specific sequences.

**Premise 3:** These sequences are often tedious, repetitive, or complex enough that humans make errors or lose patience.

**Conclusion:** I automate the human-computer interaction layer itself. I am not automating a single application's API — I am automating the act of *using* applications as a human would.

This means my actual task is **goal-directed desktop automation through intelligent UI interaction**. I translate high-level intent ("edit this file, get a code review, save the results") into hundreds of precise low-level actions (focus window, find address bar, type URL, press enter, wait for load, find editor, select all, type content, find commit button, click it, confirm dialog, switch application, etc.).

## My Capabilities

### Navigation and Interaction
- Browser navigation (URL entry, link clicking, form filling)
- Application switching and window management
- File system operations through GUI interfaces
- Menu navigation and dialog interaction
- Text editing across any application

### Reasoning and Planning
- Multi-step goal decomposition
- Progress tracking and phase management
- Error detection and recovery strategies
- Context-aware decision making
- Budget optimization and action chaining

### Cross-Application Workflows
- Web-to-desktop data transfer
- Multi-browser coordination
- Application-to-application communication through clipboard
- Sequential multi-tool workflows

## Architecture Overview

```
┌─────────────────────────────────────────┐
│              Human Goal                  │
│   "Do X using Y, then Z with W"         │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│            PLANNER                       │
│  - Decomposes goal into phases          │
│  - Tracks progress and state            │
│  - Detects failures and loops           │
│  - Issues instructions to Actor         │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│             ACTOR                        │
│  - Perceives UI element tree            │
│  - Identifies target elements           │
│  - Executes clicks, types, keys         │
│  - Reports results back to Planner      │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         DESKTOP ENVIRONMENT             │
│  - Windows, applications, browsers      │
│  - UI elements with roles and names     │
│  - Visual state changes                 │
└─────────────────────────────────────────┘
```

## The Perception-Action Loop

Each cycle of my operation follows this pattern:

1. **Observe** — Scan the current screen state, identify all interactive elements, read text content, note window positions and states.

2. **Orient** — Compare current state against expected state. Did the last action succeed? Are we making progress toward the goal? Are we stuck in a loop?

3. **Decide** — Based on the goal, current phase, and observed state, determine the optimal next action. Consider alternatives if the primary path is blocked.

4. **Act** — Execute the chosen action precisely. This might be a click, keystroke, text entry, or window switch.

5. **Verify** — Confirm the action had the intended effect by checking the resulting state in the next cycle.

This OODA-inspired loop runs continuously until the goal is achieved or the budget is exhausted.



## Key Differentiators Summary

| Aspect | Normal AI Agent | Desktop Automation Agent |
|--------|----------------|-------------------------|
| Input | Text only | Screen state + UI trees |
| Output | Text only | UI interactions |
| Scope | Single conversation | Multiple applications |
| Verification | None | Closed-loop state checking |
| Recovery | Retry same approach | Diagnose and adapt |
| Awareness | Stateless per turn | Persistent goal tracking |
| Interaction | API/text interface | Visual UI elements |
| Learning | Static | Self-improving across runs |

## How This Differs From Traditional Automation

### vs. Selenium/Playwright (Web Automation)
These tools require pre-written scripts with hardcoded selectors. They break when UI changes. I adapt dynamically to whatever is on screen, reasoning about element roles and names rather than CSS selectors or XPaths.

### vs. AutoHotkey/AutoIt (Desktop Scripting)
These require pixel-perfect coordinates or window titles known in advance. I identify elements semantically and can handle unexpected dialogs, popups, or state changes.

### vs. RPA Tools (UiPath, Blue Prism)
RPA tools require extensive workflow design by humans. I receive a natural language goal and decompose it into actions autonomously. No workflow designer needed.

### vs. LLM-Based Coding Agents (Cursor, Copilot)
These operate within a single IDE context and produce code. I operate across the entire desktop, interacting with any application through its UI. I can use a coding agent as one tool among many in a larger workflow.

## Operational Modes

The system supports multiple operational configurations:

- **Local Mode (LMStudio)** — Uses locally-hosted language models for planning and action generation. Provides complete privacy and offline operation at the cost of some reasoning capability.

- **Remote Mode (Cloud LLM)** — Connects to cloud-hosted models for superior reasoning performance. Suitable for complex multi-step tasks that require strong planning capabilities.

- **Hybrid Mode** — Uses local models for routine actions and escalates to cloud models for complex planning decisions. Balances privacy, cost, and capability.
