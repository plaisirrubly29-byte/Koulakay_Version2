from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0003_coursecategory_coursecategorymembership'),
    ]

    operations = [
        migrations.AddField(
            model_name='coursecategory',
            name='image',
            field=models.ImageField(
                blank=True,
                help_text="Image d'en-tête de la carte (recommandé : 400×220 px)",
                upload_to='categories/',
                verbose_name='Image',
            ),
        ),
        migrations.AddField(
            model_name='coursecategory',
            name='description',
            field=models.CharField(
                blank=True,
                help_text="Affichée sous le nom sur la page d'accueil",
                max_length=200,
                verbose_name='Description courte',
            ),
        ),
    ]
