"""Publish monitor assets through the standard main-node release."""
from scripts.deploy_pipeline_view import manage_node_view


def manage(action):
    return manage_node_view(action,'monitor.html')
