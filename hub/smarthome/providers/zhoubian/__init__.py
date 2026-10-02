"""周边好生活 cloud-to-cloud protocol (v1.0.5) as a delegated Provider.

The platform takes an instruction in natural language and answers in words;
it lists devices with their room and whether they are online, and nothing
more. So this adapter imports the device list, polls reachability, and turns a
structured command back into a sentence for ``device/llm/control``. It never
reports a device state it did not observe: every command ends ``delegated``.
"""
