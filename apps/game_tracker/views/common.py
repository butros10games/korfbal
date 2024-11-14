def get_time_display(match_data):
    time_left = match_data.part_lenght

    # convert the seconds to minutes and seconds to display on the page make the numbers
    # look nice with the %02d
    minutes = int(time_left / 60)
    seconds = int(time_left % 60)
    return "%02d:%02d" % (minutes, seconds)
