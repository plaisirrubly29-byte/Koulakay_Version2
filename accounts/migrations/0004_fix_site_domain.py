from django.db import migrations


def fix_site_domain(apps, schema_editor):
    Site = apps.get_model('sites', 'Site')
    Site.objects.filter(id=1).update(domain='koulakay.ht', name='KouLakay')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_createsuperuser'),
        ('sites', '0002_alter_domain_unique'),
    ]

    operations = [
        migrations.RunPython(fix_site_domain, migrations.RunPython.noop),
    ]
