from allauth.account.signals import user_signed_up
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext

from .helpers import create_default_team_for_user, get_open_invitations_for_user
from .invitations import get_invitation_id_from_request, process_invitation
from .models import Invitation, Membership, Team


@receiver(user_signed_up)
def add_user_to_team(request, user, **kwargs):
    """
    Adds the user to the team if there is invitation information in the URL.
    """
    invitation_id = get_invitation_id_from_request(request)
    if invitation_id:
        try:
            invitation = Invitation.objects.get(id=invitation_id)
            process_invitation(invitation, user)
        except Invitation.DoesNotExist:
            # for now just swallow missing invitation errors
            # these should get picked up by the form validation
            pass
    elif not user.teams.exists() and not get_open_invitations_for_user(user):
        # If the sign up was from a social account, there may not be a default team, so create one unless
        # the user has open invitations
        create_default_team_for_user(user)


@receiver(post_save, sender=Membership)
def update_billing_date_on_membership_creation(sender, instance, created, **kwargs):
    """
    Track billing changes via membership creation operations
    """
    if created:
        instance.team.billing_details_last_changed = timezone.now()
        instance.team.save()


@receiver(post_delete, sender=Membership)
def update_billing_date_on_membership_deletion(sender, instance, **kwargs):
    """
    Track billing changes via membership deletion operations
    """
    instance.team.billing_details_last_changed = timezone.now()
    instance.team.save()


@receiver(m2m_changed, sender=Membership)
def update_billing_date_on_m2m_updates(sender, instance, action, **kwargs):
    """
    Track billing changes if a membership changes via M2M operations
    """
    if action in ("post_add", "post_remove", "post_clear"):
        if isinstance(instance, Team):
            team = instance
            team.billing_details_last_changed = timezone.now()
            team.save()
        else:
            # todo: unclear how to lookup the team if this was done through the user side...
            raise Exception(gettext("Updating team membership must be done through the team object!"))
