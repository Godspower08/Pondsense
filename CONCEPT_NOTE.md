# Pond Heat Alert
### A WhatsApp early-warning system for catfish farmers in Epe, Lagos
**FortyGuard Hackathon '26**

---

## 1. The Problem, in Plain Words

Catfish farmers in Epe grow their fish in ponds dug straight into the ground. On very hot days, the water in these ponds gets dangerously warm — and most farmers have no way of knowing this is happening until they see dead or struggling fish.

By then, it's too late.

## 2. Why Does Heat Even Hurt Fish?

Here's the science, but simple: hot water can't hold as much oxygen as cool water — the same way a fizzy drink goes flat faster when it's warm. Fish breathe oxygen from the water through their gills. So when the water gets too hot, there's less oxygen in it, and the fish start to suffocate, slowly. If it gets bad enough and stays that way long enough, they die.

This isn't rare — it's one of the biggest hidden causes of fish loss for small farmers, and almost nobody is warning them about it in time.

## 3. What We're Building

A system that:
1. Watches the temperature near a farmer's pond, continuously
2. Figures out how risky it's getting for the fish
3. Sends the farmer a message on WhatsApp, in plain language, telling them what's happening and what to do about it

No app to download. No dashboard to check. Just a WhatsApp message, because that's where farmers already are.

## 4. How It Actually Works — Step by Step

**Step 1: Get the temperature.**
We use a company called FortyGuard, which tracks temperature incredibly precisely — almost street by street — using satellite and weather data. This gives us the raw information: how hot is it, right now, near this exact pond.

**Step 2: Don't just look at "right now" — look at how long it's been hot.**
This is the clever part. Imagine standing in the sun. One minute in the sun does almost nothing to you. But three hours in the sun, and you're sunburned, even if it never got any hotter than that first minute — the damage built up over time.

Pond water works the same way. So instead of just checking "is it hot right now," our system adds up how much extra heat the pond has been getting over the last several hours. We call this "degree-hours" — basically, a running total of accumulated heat, not just a single snapshot.

**Step 3: Account for how deep the pond is.**
A shallow puddle heats up in the sun way faster than a full bathtub, because there's less water to absorb the heat. Ponds work the same way — a shallow pond heats up faster than a deep one, for the exact same sunny day. So we ask each farmer, once, roughly how deep their pond is (shallow / medium / deep), and we use that to adjust how sensitive the system is for their specific pond.

**Step 4: Decide the risk level.**
Combining the accumulated heat and the pond depth, the system sorts the situation into one of four levels:
- 🟢 **Safe** — everything's normal
- 🟡 **Watch** — starting to warm up, keep an eye on it
- 🟠 **Alert** — genuinely risky, do something now
- 🔴 **Danger** — critical, urgent action needed

**Step 5: Message the farmer.**
The farmer gets a short WhatsApp message matching that risk level — plain language, no jargon, and a clear action if one's needed (like "stir the water to add oxygen" if there's no aeration equipment).

## 5. What the Farmer Actually Sees

It starts with a short one-time setup: the farmer messages the WhatsApp number, answers two quick questions (how deep is your pond, and where is it), and that's it — registered. From then on, they just receive messages when something changes:

> 🟡 **Watch** — Your pond water is starting to warm up. Sun has been strong since 11am. Check aeration this afternoon, no need to panic yet.

> 🟠 **Alert** — Heat has been building for 4 hours — your fish are stressed. Turn on aeration now if you can. Avoid feeding until evening.

They can even reply and ask questions, like "how long will this last?" and get a real answer back.

## 6. What Makes This Different

Most weather-alert tools just say "it's hot today." Ours is different in two specific ways:
- It tracks **accumulated** heat over time, not just the current reading — catching slow-building danger earlier, before the peak actually hits.
- It adjusts for **each farmer's specific pond**, instead of treating every pond the same way.

## 7. Where This Could Go

Beyond the hackathon, the honest path forward isn't charging individual farmers directly — most can't easily afford a subscription. Instead, this makes more sense as something cooperatives, fish feed suppliers, or agricultural insurers pay for, because they're the ones who lose money when farmers lose fish. The farmers get the alerts for free or heavily subsidized; the organizations who benefit from healthier fish stock cover the cost.

---
*Prepared for FortyGuard Hackathon '26 — Build Sprint Aug 18–30, 2026*
