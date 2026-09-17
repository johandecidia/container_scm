"""The customer-facing Settings area for the active team.

Three pages — Members, Tracking and Container settings — behind one rule: every
view here is `@scm_team_admin_required`, so the admin check is a property of the
module rather than something each view remembers. Hiding the navigation is not the
control; the decorator is.

Deliberately not a Django app. It owns no models: Members reads the team app's
`Membership` and `Invitation`, Tracking reads `integrations.Integration` and its
credential service, Container settings reads `containers.ContainerCondition`, and
the team tracking default lives in `tracking` beside the routing that reads it.
A settings app with its own tables would be a second place those things are
configured.
"""
