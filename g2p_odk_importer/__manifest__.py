# Part of openG2P. See LICENSE file for full copyright and licensing details.

{
    "name": "G2P ODK Importer",
    "category": "Connector",
    "summary": "Import records from ODK",
    "version": "17.0.1.2.0",
    "sequence": 3,
    "author": "OpenG2P",
    "website": "https://openg2p.org",
    "license": "LGPL-3",
    "depends": [
        "queue_job",
        "g2p_documents",
        "g2p_registry_base",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/odk_config_views.xml",
        "views/odk_menu.xml",
        "data/odk_cron.xml",
    ],
    "external_dependencies": {
        "python": [
            "pyjq",
        ]
    },
    "application": True,
    "installable": True,
    "auto_install": False,
}
